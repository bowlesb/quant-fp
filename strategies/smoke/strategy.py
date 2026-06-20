"""The smoke strategy — the FIRST strategy container. Deliberately trivial alpha (none, really):
its job is to PROVE the operational apparatus end-to-end before we add real edge.

What it does each loop:
  1. ``BusConsumer.poll()`` for its declared liquid symbols; log a "sample" of a couple real features
     so we can eyeball that live data flows (``ret_1m``, ``volume_zscore_5m``).
  2. On a fixed cadence, during market hours, under the risk caps, place ONE tiny PAPER NOTIONAL market
     buy (``notional`` = SMOKE_NOTIONAL_USD, so a $50 bet costs ~$50 regardless of share price — not a
     whole share) on the most-recently-seen symbol, ``client_order_id`` prefixed ``smoke_`` (namespacing
     for the shared paper account / a future allocation layer).
  3. MANAGE + FINALIZE: when a bet's hold expires, submit the closing sell, capture fills, compute
     realized PnL, move it OPEN -> CLOSED in the bet store.
  4. SELF-MAINTAIN: on startup, reconcile the bet store against the broker and resume managing open
     bets (closing any already past their hold) — so it survives restarts idempotently.

Risk caps (fail-safe, all enforced before any order): max concurrent bets, max total open notional
(bounding ACTUAL dollar exposure — sum of open bets' real entry cost plus the prospective bet, so a
high-priced symbol can never blow the cap), a hard kill switch (SMOKE_ENABLED=0 -> consume + log but
place NO orders), and market-hours-only.

Transient bus/broker/db errors must NOT crash the loop: we catch the SPECIFIC client exceptions and
continue. We never catch bare Exception.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from dataclasses import dataclass
from typing import cast

import psycopg
import redis
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Clock, Order
from alpaca.trading.requests import MarketOrderRequest

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.view import FeatureView
from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.paper_alpaca_executor import PaperAlpacaExecutor
from quantlib.strategy_core.production_execution import ProductionOrderIntent, make_client_order_id
from quantlib.strategy_core.production_state import PgStateStore
from strategies.lib.model import MockMLModel, Model
from strategies.lib.stale_entry import StaleEntryTracker
from strategies.smoke.bet_store import BetStore
from strategies.smoke.contract import (
    MODEL_FOLD_FEATURES,
    SAMPLE_FEATURES,
    STRATEGY_FEATURES,
    STRATEGY_NAME,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("smoke-strategy")

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "SPY", "AMD"]
COID_PREFIX = "smoke_"
EXIT_SUFFIX = "_exit"
ORDER_NOT_FOUND_CODE = 40410000


def exit_coid_for(entry_order_id: str, retry: int = 0) -> str:
    """The close order's client_order_id for a bet's entry. ``retry`` > 0 appends ``_rN`` so a
    re-submitted exit (after the prior one was lost at the broker) gets a FRESH, unique coid and can
    never collide with the dead order in the broker's idempotency history."""
    if retry <= 0:
        return f"{entry_order_id}{EXIT_SUFFIX}"
    return f"{entry_order_id}{EXIT_SUFFIX}_r{retry}"


def exit_retry_count(exit_order_id: str) -> int:
    """Parse the retry generation from an exit coid (0 for the base ``..._exit``, N for ``..._exit_rN``)."""
    marker = f"{EXIT_SUFFIX}_r"
    if marker in exit_order_id:
        return int(exit_order_id.rsplit("_r", 1)[1])
    return 0


def is_order_not_found(exc: APIError) -> bool:
    """True iff this APIError is Alpaca's "order not found for <coid>" (code 40410000).

    Distinguishes "the broker has never heard of this order" (our recorded exit SELL never actually
    landed — e.g. its submit hit a 5xx AFTER we marked the bet 'closing') from a transient lookup
    failure. We read the structured ``code`` and fall back to the message text if it is unparseable.
    """
    try:
        return int(exc.code) == ORDER_NOT_FOUND_CODE
    except (ValueError, KeyError, TypeError):
        return "order not found" in str(exc).lower()


def _env_symbols() -> list[str]:
    raw = os.environ.get("SMOKE_SYMBOLS", "").strip()
    if not raw:
        return list(DEFAULT_SYMBOLS)
    return [token.strip().upper() for token in raw.split(",") if token.strip()]


@dataclass(frozen=True)
class SmokeConfig:
    """Runtime configuration + risk caps, all overridable via env (fail-safe defaults)."""

    symbols: list[str]
    bet_interval_sec: int
    notional_usd: float
    hold_sec: int
    max_concurrent: int
    max_total_notional_usd: float
    enabled: bool
    loop_block_ms: int
    use_model: bool
    model_threshold: float

    @classmethod
    def from_env(cls) -> SmokeConfig:
        return cls(
            symbols=_env_symbols(),
            bet_interval_sec=int(os.environ.get("SMOKE_BET_INTERVAL_SEC", "300")),
            notional_usd=float(os.environ.get("SMOKE_NOTIONAL_USD", "50")),
            hold_sec=int(os.environ.get("SMOKE_HOLD_SEC", "900")),
            max_concurrent=int(os.environ.get("SMOKE_MAX_CONCURRENT", "3")),
            max_total_notional_usd=float(os.environ.get("SMOKE_MAX_TOTAL_NOTIONAL_USD", "200")),
            enabled=os.environ.get("SMOKE_ENABLED", "1") != "0",
            loop_block_ms=int(os.environ.get("SMOKE_LOOP_BLOCK_MS", "1000")),
            use_model=os.environ.get("SMOKE_USE_MODEL", "0") == "1",
            model_threshold=float(os.environ.get("SMOKE_MODEL_THRESHOLD", "0.5")),
        )


@dataclass(frozen=True)
class BetGate:
    """The decision: may we place a new bet this cycle, and why not if not?"""

    allowed: bool
    reason: str


def evaluate_bet_gate(
    config: SmokeConfig,
    now: dt.datetime,
    last_bet_ts: dt.datetime | None,
    open_count: int,
    open_notional: float,
    market_open: bool,
    last_symbol: str | None,
) -> BetGate:
    """Pure gate logic (unit-testable, no I/O): enforce kill switch, market hours, cadence, and the
    concurrency + total-notional caps before any order is placed. The prospective new bet's notional
    is added to the current open notional so the cap is checked INCLUSIVE of the bet we'd place."""
    if not config.enabled:
        return BetGate(False, "kill_switch_off")
    if not market_open:
        return BetGate(False, "market_closed")
    if last_symbol is None:
        return BetGate(False, "no_symbol_seen")
    if last_bet_ts is not None and (now - last_bet_ts).total_seconds() < config.bet_interval_sec:
        return BetGate(False, "within_cadence")
    if open_count >= config.max_concurrent:
        return BetGate(False, "max_concurrent")
    if open_notional + config.notional_usd > config.max_total_notional_usd:
        return BetGate(False, "max_total_notional")
    return BetGate(True, "ok")


def sample_features(vector: object) -> dict[str, float]:
    """Read the configured sample features off a decoded vector for eyeball logging. A vector missing a
    sample feature would raise KeyError from ``value`` — we want that loud (it means schema drift)."""
    return {name: vector.value(name) for name in SAMPLE_FEATURES}  # type: ignore[attr-defined]


def order_filled_qty(order: Order) -> float | None:
    """The actual (fractional) share count an order filled, or None if not yet filled.

    A notional BUY fills an unknown fractional quantity; we read it from ``filled_qty`` so the close
    can sell exactly that many shares. A zero/blank fill is treated as not-yet-filled.
    """
    filled_qty = order.filled_qty
    if filled_qty is None:
        return None
    qty = float(filled_qty)
    return qty if qty > 0 else None


class SmokeStrategy:
    """Owns the consume -> bet -> manage -> finalize loop and the broker/store/bus handles."""

    def __init__(
        self,
        config: SmokeConfig,
        consumer: BusConsumer,
        trading: TradingClient,
        store: BetStore,
        model: Model | None = None,
        state_store: PgStateStore | None = None,
    ) -> None:
        self._config = config
        self._consumer = consumer
        self._trading = trading
        self._store = store
        # the production execution+state layer, running ALONGSIDE the bespoke bet-store (which is retained
        # unchanged for backward-readability + clean rollback): broker calls go through PaperAlpacaExecutor,
        # and every captured fill is also booked into the durable StrategyState ledger (the SoT migration).
        self._executor = PaperAlpacaExecutor(trading)
        self._state_store = state_store
        self._state = state_store.load(STRATEGY_NAME) if state_store is not None else None
        self._model = model if model is not None else MockMLModel(MODEL_FOLD_FEATURES)
        self._last_symbol: str | None = None
        self._last_vector: FeatureView | None = None
        self._last_bet_ts: dt.datetime | None = None
        # bounded detector for entries whose order never landed (genuinely not-found): stops the
        # every-tick broker re-query spin without ever expiring a live/in-flight order.
        self._stale_entries = StaleEntryTracker()
        # coids whose "CLOSE pending fill" we've already logged this state-generation, so a stale pending
        # exit on a closed market doesn't hot-log every manage tick (per-state-change, not per-tick) — the
        # same log-dedup as reversion's path. Pure legibility; zero behavior change.
        self._pending_close_logged: set[str] = set()

    def _book_fill(self, symbol: str, side: str, coid: str, qty: float, price: float) -> None:
        """Mirror a captured fill into the durable StrategyState ledger (the migration SoT). A no-op when
        no state store is wired (the bespoke bets table remains the record), so the change is additive and
        the loop's behavior/decisions are unchanged."""
        if self._state is None or self._state_store is None:
            return
        fill = Fill(
            symbol=symbol,
            side=side,
            weight=0.0,
            fill_price=price,
            cost_bps=0.0,
            client_order_id=coid,
            filled_qty=qty,
            avg_price=price,
            status=OrderState.FILLED,
        )
        if any(existing.client_order_id == coid for existing in self._state.fills):
            return  # idempotent: a coid already booked is never double-counted (restart-safe)
        self._state.apply_fill(fill)
        self._state_store.append_fill(STRATEGY_NAME, fill)

    def publish_contract(self) -> None:
        """Publish this strategy's declared (name, version) feature contract to the bus so the pre-deploy
        compat gate sees what is actually running (B3 — a strategy that doesn't publish fails the gate
        closed)."""
        self._consumer.publish_contract(STRATEGY_NAME, STRATEGY_FEATURES)
        logger.info("published feature contract for '%s': %s", STRATEGY_NAME, STRATEGY_FEATURES)

    def market_open(self) -> bool:
        """Broker clock — only trade during regular hours; outside, consume + log only."""
        clock = cast(Clock, self._trading.get_clock())
        return bool(clock.is_open)

    def reconcile_on_startup(self) -> None:
        """Resume managing bets left open by a prior run: for each store-open bet, pull its broker
        status, record a missed fill, and close anything past its hold. Idempotent (every step is a
        guarded UPDATE or an ON CONFLICT submit), so repeated startups converge to the same book."""
        open_bets = self._store.list_open()
        if not open_bets:
            logger.info("reconcile: no open bets to resume")
            return
        logger.info("reconcile: resuming %d open bet(s)", len(open_bets))
        now = dt.datetime.now(dt.timezone.utc)
        for bet in open_bets:
            entry_order_id = str(bet["entry_order_id"])
            order = self._fetch_order_by_coid(entry_order_id)
            if order is not None and bet["entry_price"] is None:
                filled_qty = order_filled_qty(order)
                if order.filled_avg_price and filled_qty is not None:
                    self._store.mark_filled(entry_order_id, float(order.filled_avg_price), filled_qty)
                    bet["entry_price"] = float(order.filled_avg_price)
                    bet["qty"] = filled_qty
            hold_until = bet["hold_until"]
            if isinstance(hold_until, dt.datetime) and now >= hold_until:
                self._close_bet(bet)

    def _fetch_order_by_coid(self, client_order_id: str) -> Order | None:
        try:
            return cast(Order, self._trading.get_order_by_client_id(client_order_id))
        except APIError as exc:
            logger.warning("reconcile: order %s not found at broker (%s)", client_order_id, exc)
            return None

    def _fetch_entry_with_status(self, client_order_id: str) -> tuple[Order | None, bool]:
        """Look up an ENTRY order, distinguishing GENUINELY not-found (40410000) from a transient error.
        Returns ``(order, genuinely_not_found)``. Only a genuine not-found feeds the stale-entry tracker;
        a transient error returns ``(None, False)`` and resets the streak so a live order is never expired.
        """
        try:
            return cast(Order, self._trading.get_order_by_client_id(client_order_id)), False
        except APIError as exc:
            if is_order_not_found(exc):
                return None, True
            logger.warning("entry lookup transient error for %s (%s)", client_order_id, exc)
            return None, False

    def _abandon_if_entry_dead(self, entry_order_id: str, symbol: str, now: dt.datetime) -> bool:
        """If a still-unfilled entry's order is GENUINELY not-found for a bounded streak (N consecutive
        not-founds over >= M seconds), terminate the bet (it never landed) and stop re-querying. Returns
        True iff the bet was abandoned this cycle. A transient miss / a found order resets the streak via
        the caller, so a live in-flight order is never wrongly expired."""
        if self._stale_entries.record_not_found(entry_order_id, now):
            logger.warning(
                "ABANDON: entry %s (%s) genuinely not-found x%d — never landed; marking bet terminal",
                entry_order_id,
                symbol,
                self._stale_entries.streak_count(entry_order_id),
            )
            self._store.mark_abandoned(entry_order_id)
            self._stale_entries.forget(entry_order_id)
            return True
        return False

    def _lookup_exit_order(self, exit_coid: str) -> tuple[Order | None, bool]:
        """Look up a close order by coid, distinguishing "not yet visible" from "does not exist".

        Returns ``(order, missing)``. ``missing`` is True only for an explicit order-not-found
        (code 40410000) — i.e. the broker has NO such order, so the SELL never landed and the bet is
        stuck. Any other APIError is a transient lookup failure and returns ``(None, False)`` so we
        simply retry next cycle rather than wrongly re-submitting.
        """
        try:
            return cast(Order, self._trading.get_order_by_client_id(exit_coid)), False
        except APIError as exc:
            if is_order_not_found(exc):
                return None, True
            logger.warning("exit lookup transient error for %s (%s)", exit_coid, exc)
            return None, False

    def consume_and_sample(self) -> None:
        """Poll the bus and log a sample of real features for the latest vector per symbol."""
        vectors = self._consumer.poll_views(block_ms=self._config.loop_block_ms, count=200)
        if not vectors:
            return
        for vector in vectors:
            self._last_symbol = vector.symbol
        latest = vectors[-1]
        self._last_vector = latest
        samples = sample_features(latest)
        rendered = " ".join(f"{name}={value:+.5f}" for name, value in samples.items())
        logger.info(
            "SAMPLE %s @ %s | %s (%d vectors this poll)",
            latest.symbol,
            latest.minute.isoformat(),
            rendered,
            len(vectors),
        )

    def _model_allows(self) -> bool:
        """Model overlay (only when SMOKE_USE_MODEL=1): predict on the latest vector and require the
        probability to clear the threshold. The mock model is deterministic per (symbol, minute), so the
        signal varies but is reproducible; a real classifier drops in behind the same ``Model`` interface.
        When the overlay is off, this is a no-op (current behaviour: bet purely on the safety gate)."""
        if not self._config.use_model:
            return True
        if self._last_vector is None:
            logger.debug("no bet: model_no_vector")
            return False
        prediction = self._model.predict(self._last_vector)
        if prediction.probability <= self._config.model_threshold:
            logger.info(
                "no bet: model p=%.3f <= threshold %.3f (%s %s)",
                prediction.probability,
                self._config.model_threshold,
                prediction.symbol,
                prediction.model,
            )
            return False
        logger.info(
            "model OK: p=%.3f > threshold %.3f (%s %s)",
            prediction.probability,
            self._config.model_threshold,
            prediction.symbol,
            prediction.model,
        )
        return True

    def maybe_place_bet(self) -> None:
        """Place one tiny paper buy if the gate allows. Records intent in the store BEFORE the order
        is acknowledged-complete, so a crash mid-place still leaves a managed (idempotent) bet."""
        now = dt.datetime.now(dt.timezone.utc)
        gate = evaluate_bet_gate(
            config=self._config,
            now=now,
            last_bet_ts=self._last_bet_ts,
            open_count=self._store.count_open(),
            open_notional=self._store.open_notional(),
            market_open=self.market_open(),
            last_symbol=self._last_symbol,
        )
        if not gate.allowed:
            logger.debug("no bet: %s", gate.reason)
            return
        if not self._model_allows():
            return
        symbol = str(self._last_symbol)
        notional = self._config.notional_usd
        coid = make_client_order_id(STRATEGY_NAME, now, symbol, "buy")
        hold_until = now + dt.timedelta(seconds=self._config.hold_sec)
        self._store.record_open(symbol, "buy", notional, coid, now, hold_until)
        intent = ProductionOrderIntent(
            strategy_id=STRATEGY_NAME, symbol=symbol, side="buy", decision_ts=now, notional=notional
        )
        record = self._executor.submit(intent)
        self._last_bet_ts = now
        logger.info(
            "BET placed: BUY $%.2f notional %s coid=%s broker_id=%s hold=%ds",
            notional,
            symbol,
            coid,
            record.broker_order_id,
            self._config.hold_sec,
        )

    def manage_open_bets(self) -> None:
        """Capture fills on still-open entries and finalize any bet past its hold."""
        now = dt.datetime.now(dt.timezone.utc)
        for bet in self._store.list_open():
            entry_order_id = str(bet["entry_order_id"])
            if bet["entry_price"] is None:
                order, not_found = self._fetch_entry_with_status(entry_order_id)
                if not_found:
                    # the entry order genuinely doesn't exist at the broker; after a bounded streak,
                    # terminate the bet (it never landed) and stop re-querying it every tick.
                    if self._abandon_if_entry_dead(entry_order_id, str(bet["symbol"]), now):
                        continue
                    continue  # still within the streak window — re-check next cycle, no other action
                self._stale_entries.reset(entry_order_id)  # found / transient -> clear the not-found streak
                if order is not None and order.filled_avg_price:
                    filled_qty = order_filled_qty(order)
                    if filled_qty is not None:
                        filled_price = float(order.filled_avg_price)
                        self._store.mark_filled(entry_order_id, filled_price, filled_qty)
                        self._book_fill(str(bet["symbol"]), "buy", entry_order_id, filled_qty, filled_price)
                        bet["entry_price"] = filled_price
                        bet["qty"] = filled_qty
            hold_until = bet["hold_until"]
            if isinstance(hold_until, dt.datetime) and now >= hold_until:
                self._close_bet(bet)

    def _ensure_entry_filled(self, bet: dict[str, object]) -> tuple[float, float] | None:
        """Return (entry_price, filled_qty) for a bet's open, capturing the fill into the store if it
        landed since we last looked. Returns None if the notional entry has not filled yet — we must
        NOT submit the close until we know how many (fractional) shares we actually hold."""
        entry_order_id = str(bet["entry_order_id"])
        entry_price = bet["entry_price"]
        qty = bet["qty"]
        if entry_price is not None and qty is not None:
            return float(cast(float, entry_price)), float(cast(float, qty))
        entry_order = self._fetch_order_by_coid(entry_order_id)
        if entry_order is None or not entry_order.filled_avg_price:
            return None
        filled_qty = order_filled_qty(entry_order)
        if filled_qty is None:
            return None
        entry_price_value = float(entry_order.filled_avg_price)
        self._store.mark_filled(entry_order_id, entry_price_value, filled_qty)
        self._book_fill(str(bet["symbol"]), "buy", entry_order_id, filled_qty, entry_price_value)
        bet["entry_price"] = entry_price_value
        bet["qty"] = filled_qty
        return entry_price_value, filled_qty

    def _submit_exit(self, symbol: str, qty: float, entry_order_id: str, exit_coid: str) -> None:
        """Submit the closing SELL for ``qty`` shares under ``exit_coid``. A coid collision means the
        close is already in flight at the broker, so we swallow it and read the fill next; any other
        APIError (e.g. a 5xx) propagates — the bet stays 'closing' with this coid recorded, and the
        not-found resolver re-submits a fresh exit on a later cycle rather than looping forever."""
        try:
            self._trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=exit_coid,
                )
            )
            logger.info("CLOSE submitted: SELL %g %s coid=%s", qty, symbol, exit_coid)
        except APIError as exc:
            if "client_order_id" not in str(exc) and "unique" not in str(exc).lower():
                raise
            logger.warning("CLOSE coid %s already submitted; reading its fill", exit_coid)

    def _resubmit_lost_exit(
        self, bet: dict[str, object], symbol: str, qty: float
    ) -> tuple[Order | None, str]:
        """Recover a bet whose recorded exit order does NOT exist at the broker (the SELL never landed,
        e.g. its submit hit an Alpaca 5xx after we marked the bet 'closing'). The position is still
        open, so we re-submit a FRESH exit (new coid, next retry generation), persist the new handle,
        and return its (as-yet-unfilled) order so the close resolves on the next cycle instead of
        looping on the lost order forever. Returns ``(order, exit_coid)``."""
        entry_order_id = str(bet["entry_order_id"])
        previous_exit = str(bet["exit_order_id"])
        retry = exit_retry_count(previous_exit) + 1
        new_coid = exit_coid_for(entry_order_id, retry)
        logger.warning(
            "CLOSE exit %s not found at broker; re-submitting fresh exit %s for %s",
            previous_exit,
            new_coid,
            symbol,
        )
        self._store.update_exit_coid(entry_order_id, new_coid)
        bet["exit_order_id"] = new_coid
        self._submit_exit(symbol, qty, entry_order_id, new_coid)
        return self._fetch_order_by_coid(new_coid), new_coid

    def _close_bet(self, bet: dict[str, object]) -> None:
        """Submit the closing sell (idempotent coid), capture the exit fill, compute realized PnL,
        and move the bet to CLOSED. Re-runs safely: a coid collision means the close is already in
        flight, so we just try to read its fill. If a previously-recorded exit order is NOT FOUND at
        the broker (the SELL never landed), we re-submit a fresh exit rather than loop on the lost one.

        The close sells the actual filled (fractional) ``qty`` of the notional entry, so we only
        proceed once that quantity is known — otherwise we wait for the entry to fill."""
        entry_order_id = str(bet["entry_order_id"])
        symbol = str(bet["symbol"])
        filled = self._ensure_entry_filled(bet)
        if filled is None:
            logger.info("CLOSE deferred: entry not yet filled for %s (coid=%s)", symbol, entry_order_id)
            return
        entry_price_value, qty = filled
        existing_exit = bet["exit_order_id"]
        if existing_exit is None:
            exit_coid = exit_coid_for(entry_order_id)
            self._store.mark_closing(entry_order_id, exit_coid)
            self._submit_exit(symbol, qty, entry_order_id, exit_coid)
        else:
            exit_coid = str(existing_exit)
        exit_order, missing = self._lookup_exit_order(exit_coid)
        if missing:
            exit_order, exit_coid = self._resubmit_lost_exit(bet, symbol, qty)
        if exit_order is None or not exit_order.filled_avg_price:
            # log a pending exit ONCE per coid (not every ~1.4s manage tick) so a stale pending close on a
            # closed market doesn't hot-log forever — purely a log-legibility fix, no behavior change.
            if exit_coid not in self._pending_close_logged:
                logger.info("CLOSE pending fill for %s (coid=%s)", symbol, exit_coid)
                self._pending_close_logged.add(exit_coid)
            return
        exit_price = float(exit_order.filled_avg_price)
        realized = (exit_price - entry_price_value) * qty
        exit_ts = exit_order.filled_at or dt.datetime.now(dt.timezone.utc)
        self._store.record_close(entry_order_id, exit_ts, exit_price, realized)
        self._book_fill(symbol, "sell", exit_coid, qty, exit_price)
        self._pending_close_logged.discard(exit_coid)  # resolved -> forget its pending-log marker
        logger.info(
            "BET closed: %s entry=%.4f exit=%.4f qty=%g pnl=%+.4f",
            symbol,
            entry_price_value,
            exit_price,
            qty,
            realized,
        )

    def cycle(self) -> None:
        """One full loop iteration: consume+sample, manage existing bets, maybe open a new one."""
        self.consume_and_sample()
        self.manage_open_bets()
        self.maybe_place_bet()

    def run(self) -> None:
        logger.info(
            "smoke strategy starting: symbols=%s enabled=%s interval=%ds notional=$%.0f hold=%ds "
            "caps[concurrent=%d total_notional=$%.0f] model[use=%s threshold=%.2f]",
            self._config.symbols,
            self._config.enabled,
            self._config.bet_interval_sec,
            self._config.notional_usd,
            self._config.hold_sec,
            self._config.max_concurrent,
            self._config.max_total_notional_usd,
            self._config.use_model,
            self._config.model_threshold,
        )
        self.publish_contract()
        self.reconcile_on_startup()
        while True:
            try:
                self.cycle()
            except redis.exceptions.RedisError as exc:
                logger.warning("bus error (continuing): %s", exc)
                time.sleep(1.0)
            except APIError as exc:
                logger.warning("broker error (continuing): %s", exc)
                time.sleep(1.0)
            except psycopg.Error as exc:
                logger.warning("db error (continuing): %s", exc)
                time.sleep(1.0)
