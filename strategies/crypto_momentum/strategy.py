"""The crypto-momentum strategy — the FIRST live CRYPTO strategy container.

Its job is to demonstrate the FULL end-to-end loop on the 24/7 crypto stream — bar -> feature vector ->
strategy -> PAPER crypto order -> manage/finalize — so we catch process/integration issues off-hours NOW,
instead of waiting for Monday equities. It is NOT an edge claim: the signal is a deliberately trivial
short-horizon momentum continuation (long when ``ret_{w}m`` is sufficiently positive). Everything is
PAPER-only and tiny.

It is the crypto sibling of the reversion strategy, with three crypto-specific adaptations and NOTHING
else changed in philosophy:
  1. BUS — it consumes the SEPARATE ``fv:crypto:<SYMBOL>`` namespace (``BusConsumer(prefix="fv:crypto")``)
     with SLASHLESS symbols (``BTCUSD``). It never reads the equity ``fv:<symbol>`` streams.
  2. 24/7 — crypto trades continuously, so there is NO market-hours gate (no broker clock). The cadence +
     risk caps are the only throttle.
  3. ORDERS — Alpaca crypto orders use the SLASH pair (``BTC/USD``) and reject ``TimeInForce.DAY``; we map
     the slashless bus symbol -> the slash Alpaca symbol ONLY at the order boundary and submit with
     ``TimeInForce.GTC``. The coid + the bet ledger keep the clean slashless symbol (so the G2 coid parses).

What it does each loop:
  1. ``BusConsumer.poll_views()`` for its declared crypto pairs; keep the LATEST FeatureView per symbol,
     resolved BY NAME against the producer's published schema (the #210/#211 decoupling — the consumer
     reads ``ret_{w}m`` by name regardless of the producer's feature-set layout).
  2. MANAGE + FINALIZE: capture entry fills; when a bet's hold expires, submit the closing sell, capture
     the exit fill, compute realized PnL, move OPEN -> CLOSED.
  3. SCORE + maybe place: rank held-eligible symbols by the model's momentum probability; if the best
     clears the threshold and the safety gate allows, place one notional long on it.
  4. SELF-MAINTAIN: reconcile the bet store against the broker on startup; resume managing open bets.

Risk caps (identical philosophy to reversion, env prefix ``CMOM_``): kill switch, cadence, max concurrent,
max total open notional (bounds ACTUAL exposure inclusive of the prospective bet), plus one-bet-per-symbol.
Transient bus/broker/db errors are caught specifically and the loop continues; never a bare except.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from dataclasses import dataclass
from typing import cast

import numpy as np
import psycopg
import redis
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import MarketOrderRequest

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.view import FeatureView
from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.production_execution import make_client_order_id
from quantlib.strategy_core.production_state import PgStateStore
from strategies.crypto_momentum.bet_store import BetStore
from strategies.crypto_momentum.contract import STRATEGY_NAME, contract_for
from strategies.lib.crypto_momentum_model import CryptoMomentumModel
from strategies.lib.stale_entry import StaleEntryTracker, is_order_not_found

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crypto-momentum-strategy")

DEFAULT_SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD", "LTCUSD", "DOGEUSD"]
CRYPTO_BUS_PREFIX = "fv:crypto"
EXIT_SUFFIX = "_exit"


def to_alpaca_symbol(bus_symbol: str) -> str:
    """Map a SLASHLESS bus/store crypto symbol (``BTCUSD``) to the SLASH Alpaca pair (``BTC/USD``).

    Alpaca crypto pairs are ``BASE/QUOTE`` and all crypto-capture pairs quote in USD, so we split off the
    trailing ``USD`` and re-insert the slash. A symbol that does not end in ``USD`` is returned unchanged
    (it is then up to the broker to reject it loudly), but every configured pair is a ``*USD`` pair."""
    if bus_symbol.endswith("USD") and len(bus_symbol) > 3:
        return f"{bus_symbol[:-3]}/USD"
    return bus_symbol


def _env_symbols() -> list[str]:
    raw = os.environ.get("CMOM_SYMBOLS", "").strip()
    if not raw:
        return list(DEFAULT_SYMBOLS)
    # accept either slashless (BTCUSD) or slash (BTC/USD) in env; normalize to the slashless bus form.
    return [token.strip().upper().replace("/", "") for token in raw.split(",") if token.strip()]


@dataclass(frozen=True)
class CryptoMomentumConfig:
    """Runtime configuration + risk caps, all overridable via env (fail-safe defaults)."""

    symbols: list[str]
    bet_interval_sec: int
    notional_usd: float
    hold_sec: int
    max_concurrent: int
    max_total_notional_usd: float
    enabled: bool
    loop_block_ms: int
    ret_window_m: int
    sensitivity: float
    threshold: float

    @classmethod
    def from_env(cls) -> CryptoMomentumConfig:
        return cls(
            symbols=_env_symbols(),
            bet_interval_sec=int(os.environ.get("CMOM_BET_INTERVAL_SEC", "300")),
            notional_usd=float(os.environ.get("CMOM_NOTIONAL_USD", "50")),
            hold_sec=int(os.environ.get("CMOM_HOLD_SEC", "1800")),
            max_concurrent=int(os.environ.get("CMOM_MAX_CONCURRENT", "3")),
            max_total_notional_usd=float(os.environ.get("CMOM_MAX_TOTAL_NOTIONAL_USD", "200")),
            enabled=os.environ.get("CMOM_ENABLED", "1") != "0",
            loop_block_ms=int(os.environ.get("CMOM_LOOP_BLOCK_MS", "1000")),
            ret_window_m=int(os.environ.get("CMOM_RET_WINDOW_M", "5")),
            sensitivity=float(os.environ.get("CMOM_SENSITIVITY", "200")),
            threshold=float(os.environ.get("CMOM_THRESHOLD", "0.55")),
        )


@dataclass(frozen=True)
class BetGate:
    """The decision: may we place a new bet this cycle (cap/cadence), and why not if not?"""

    allowed: bool
    reason: str


def evaluate_bet_gate(
    config: CryptoMomentumConfig,
    now: dt.datetime,
    last_bet_ts: dt.datetime | None,
    open_count: int,
    open_notional: float,
) -> BetGate:
    """Pure gate logic (unit-testable, no I/O): kill switch, cadence, concurrency cap, and total-notional
    cap (INCLUSIVE of the prospective bet's notional). There is NO market-hours gate — crypto is 24/7. The
    per-symbol candidate selection + threshold is applied separately (``select_candidate``)."""
    if not config.enabled:
        return BetGate(False, "kill_switch_off")
    if last_bet_ts is not None and (now - last_bet_ts).total_seconds() < config.bet_interval_sec:
        return BetGate(False, "within_cadence")
    if open_count >= config.max_concurrent:
        return BetGate(False, "max_concurrent")
    if open_notional + config.notional_usd > config.max_total_notional_usd:
        return BetGate(False, "max_total_notional")
    return BetGate(True, "ok")


@dataclass(frozen=True)
class Candidate:
    """A scored long candidate: the symbol, its momentum probability, and the raw return read."""

    symbol: str
    probability: float
    ret: float


def select_candidate(
    model: CryptoMomentumModel,
    latest_by_symbol: dict[str, FeatureView],
    threshold: float,
    excluded: set[str],
) -> Candidate | None:
    """Score every symbol's latest vector and return the single best long candidate (highest momentum
    probability) that clears ``threshold`` and is not ``excluded`` (already held). Pure: no I/O, no
    wall-clock — deterministic in the vectors. A non-finite feature yields P=0.5 (below any sane
    threshold), so warmup/sparse names are naturally skipped. Returns None when nothing qualifies."""
    best: Candidate | None = None
    for symbol, vector in latest_by_symbol.items():
        if symbol in excluded:
            continue
        prediction = model.predict(vector)
        if prediction.probability <= threshold:
            continue
        ret = vector.value(model.feature_name)
        if not np.isfinite(ret):
            continue
        if best is None or prediction.probability > best.probability:
            best = Candidate(symbol=symbol, probability=prediction.probability, ret=float(ret))
    return best


def order_filled_qty(order: Order) -> float | None:
    """The actual (fractional) coin count an order filled, or None if not yet filled."""
    filled_qty = order.filled_qty
    if filled_qty is None:
        return None
    qty = float(filled_qty)
    return qty if qty > 0 else None


class CryptoMomentumStrategy:
    """Owns the consume -> score -> bet -> manage -> finalize loop and the broker/store/bus handles."""

    def __init__(
        self,
        config: CryptoMomentumConfig,
        consumer: BusConsumer,
        trading: TradingClient,
        store: BetStore,
        model: CryptoMomentumModel,
        state_store: PgStateStore | None = None,
    ) -> None:
        self._config = config
        self._consumer = consumer
        self._trading = trading
        self._store = store
        self._state_store = state_store
        self._state = state_store.load(STRATEGY_NAME) if state_store is not None else None
        self._model = model
        self._latest_by_symbol: dict[str, FeatureView] = {}
        self._last_bet_ts: dt.datetime | None = None
        # coids whose "CLOSE pending fill" we've already logged this state-generation, so a stale pending
        # exit doesn't hot-log every manage tick (per-state-change, not per-tick).
        self._pending_close_logged: set[str] = set()
        # bounded detector for entries whose order never landed (genuinely not-found): stops the
        # every-tick broker re-query spin without ever expiring a live/in-flight order.
        self._stale_entries = StaleEntryTracker()

    def _book_fill(self, symbol: str, side: str, coid: str, qty: float, price: float) -> None:
        """Mirror a captured fill into the durable StrategyState ledger (the SoT). A no-op when no state
        store is wired, so the change is additive and the loop's decisions are unchanged. ``symbol`` is the
        slashless bus form, kept consistent across the ledger."""
        if self._state is None or self._state_store is None:
            return
        if any(existing.client_order_id == coid for existing in self._state.fills):
            return  # idempotent: a coid already booked is never double-counted (restart-safe)
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
        self._state.apply_fill(fill)
        self._state_store.append_fill(STRATEGY_NAME, fill)

    def publish_contract(self) -> None:
        """Publish this strategy's declared (name, version) feature contract — derived from the constructed
        model — so the pre-deploy compat gate reads what is actually running (B3)."""
        contract = contract_for(self._model)
        self._consumer.publish_contract(STRATEGY_NAME, contract)
        logger.info("published feature contract for '%s': %s", STRATEGY_NAME, contract)

    def reconcile_on_startup(self) -> None:
        """Resume managing bets left open by a prior run: capture any missed entry fill and close anything
        past its hold. Idempotent (guarded UPDATEs + ON CONFLICT submits), so restarts converge."""
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
                    filled_price = float(order.filled_avg_price)
                    self._store.mark_filled(entry_order_id, filled_price, filled_qty)
                    self._book_fill(str(bet["symbol"]), "buy", entry_order_id, filled_qty, filled_price)
                    bet["entry_price"] = filled_price
                    bet["qty"] = filled_qty
            hold_until = bet["hold_until"]
            if isinstance(hold_until, dt.datetime) and now >= hold_until:
                self._close_bet(bet)

    def _fetch_order_by_coid(self, client_order_id: str) -> Order | None:
        try:
            return cast(Order, self._trading.get_order_by_client_id(client_order_id))
        except APIError as exc:
            logger.warning("order %s not found at broker (%s)", client_order_id, exc)
            return None

    def _fetch_entry_with_status(self, client_order_id: str) -> tuple[Order | None, bool]:
        """Look up an ENTRY order, distinguishing GENUINELY not-found (40410000) from a transient error.
        Returns ``(order, genuinely_not_found)`` — only a genuine not-found feeds the stale-entry tracker."""
        try:
            return cast(Order, self._trading.get_order_by_client_id(client_order_id)), False
        except APIError as exc:
            if is_order_not_found(exc):
                return None, True
            logger.warning("entry lookup transient error for %s (%s)", client_order_id, exc)
            return None, False

    def _abandon_if_entry_dead(self, entry_order_id: str, symbol: str, now: dt.datetime) -> bool:
        """Terminate a bet whose unfilled entry is GENUINELY not-found for a bounded streak (N consecutive
        over >= M seconds) and stop re-querying it. Returns True iff abandoned this cycle."""
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

    def _submit_market(
        self,
        bus_symbol: str,
        side: OrderSide,
        coid: str,
        *,
        notional: float | None = None,
        qty: float | None = None,
    ) -> None:
        """Submit a crypto market order. Maps the slashless bus symbol -> the slash Alpaca pair and uses
        ``TimeInForce.GTC`` (crypto rejects DAY). Exactly one of ``notional`` / ``qty`` is set. A
        duplicate-coid APIError is swallowed (the order is already in flight; we read its fill next);
        any other APIError propagates."""
        alpaca_symbol = to_alpaca_symbol(bus_symbol)
        kwargs: dict[str, object] = {
            "symbol": alpaca_symbol,
            "side": side,
            "time_in_force": TimeInForce.GTC,
            "client_order_id": coid,
        }
        if notional is not None:
            kwargs["notional"] = notional
        else:
            kwargs["qty"] = qty
        try:
            self._trading.submit_order(MarketOrderRequest(**kwargs))  # type: ignore[arg-type]
        except APIError as exc:
            if "client_order_id" not in str(exc) and "unique" not in str(exc).lower():
                raise
            logger.warning("order coid %s already submitted; reading its fill", coid)

    def consume(self) -> None:
        """Poll the crypto bus and keep the LATEST vector per symbol (the score set for this cycle)."""
        vectors = self._consumer.poll_views(block_ms=self._config.loop_block_ms, count=200)
        if not vectors:
            return
        for vector in vectors:
            self._latest_by_symbol[vector.symbol] = vector
        latest = vectors[-1]
        ret = latest.value(self._model.feature_name)
        logger.info(
            "SAMPLE %s @ %s | %s=%+.5f (%d vectors this poll, %d symbols tracked)",
            latest.symbol,
            latest.minute.isoformat(),
            self._model.feature_name,
            ret if np.isfinite(ret) else float("nan"),
            len(vectors),
            len(self._latest_by_symbol),
        )

    def maybe_place_bet(self) -> None:
        """Score candidates and place one notional long on the highest-momentum qualifying pair (under the
        safety gate). Records intent in the store BEFORE the order completes, so a crash mid-place still
        leaves a managed (idempotent) bet."""
        now = dt.datetime.now(dt.timezone.utc)
        gate = evaluate_bet_gate(
            config=self._config,
            now=now,
            last_bet_ts=self._last_bet_ts,
            open_count=self._store.count_open(),
            open_notional=self._store.open_notional(),
        )
        if not gate.allowed:
            logger.debug("no bet: %s", gate.reason)
            return
        excluded = {bet["symbol"] for bet in self._store.list_open() if isinstance(bet["symbol"], str)}
        candidate = select_candidate(
            self._model, self._latest_by_symbol, self._config.threshold, cast("set[str]", excluded)
        )
        if candidate is None:
            logger.debug("no bet: no candidate clears threshold %.2f", self._config.threshold)
            return
        symbol = candidate.symbol
        notional = self._config.notional_usd
        coid = make_client_order_id(STRATEGY_NAME, now, symbol, "buy")
        hold_until = now + dt.timedelta(seconds=self._config.hold_sec)
        self._store.record_open(symbol, "buy", notional, coid, now, hold_until, candidate.probability)
        self._submit_market(symbol, OrderSide.BUY, coid, notional=notional)
        self._last_bet_ts = now
        logger.info(
            "BET placed: BUY $%.2f notional %s (%s) p=%.3f ret=%+.5f coid=%s hold=%ds",
            notional,
            symbol,
            to_alpaca_symbol(symbol),
            candidate.probability,
            candidate.ret,
            coid,
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
                    self._abandon_if_entry_dead(entry_order_id, str(bet["symbol"]), now)
                    continue
                self._stale_entries.reset(entry_order_id)  # found / transient -> clear the streak
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
        landed since we last looked. None if the notional entry has not filled — we must NOT submit the
        close until we know how many (fractional) coins we hold."""
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

    def _close_bet(self, bet: dict[str, object]) -> None:
        """Submit the closing sell (idempotent coid), capture the exit fill, compute realized PnL, and
        move the bet to CLOSED. Re-runs safely; the close sells the actual filled fractional ``qty``, so
        we only proceed once that quantity is known."""
        entry_order_id = str(bet["entry_order_id"])
        symbol = str(bet["symbol"])
        filled = self._ensure_entry_filled(bet)
        if filled is None:
            logger.info("CLOSE deferred: entry not yet filled for %s (coid=%s)", symbol, entry_order_id)
            return
        entry_price_value, qty = filled
        exit_coid = f"{entry_order_id}{EXIT_SUFFIX}"
        existing_exit = bet["exit_order_id"]
        if existing_exit is None:
            self._store.mark_closing(entry_order_id, exit_coid)
            self._submit_market(symbol, OrderSide.SELL, exit_coid, qty=qty)
            logger.info(
                "CLOSE submitted: SELL %g %s (%s) coid=%s", qty, symbol, to_alpaca_symbol(symbol), exit_coid
            )
        else:
            exit_coid = str(existing_exit)
        exit_order = self._fetch_order_by_coid(exit_coid)
        if exit_order is None or not exit_order.filled_avg_price:
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
        """One full loop iteration: consume, manage existing bets, maybe open a new one."""
        self.consume()
        self.manage_open_bets()
        self.maybe_place_bet()

    def run(self) -> None:
        logger.info(
            "crypto-momentum strategy starting: symbols=%s enabled=%s interval=%ds notional=$%.0f hold=%ds "
            "caps[concurrent=%d total_notional=$%.0f] model[ret_window=%dm sensitivity=%.0f threshold=%.2f]",
            self._config.symbols,
            self._config.enabled,
            self._config.bet_interval_sec,
            self._config.notional_usd,
            self._config.hold_sec,
            self._config.max_concurrent,
            self._config.max_total_notional_usd,
            self._config.ret_window_m,
            self._config.sensitivity,
            self._config.threshold,
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
