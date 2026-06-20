"""The reversion strategy — the SECOND strategy container, with a REAL (if modest) signal.

It trades intraday VWAP mean-reversion: each cycle it scores every symbol's latest feature vector with
``VwapReversionModel`` (P(up) rises as a name stretches BELOW its trailing VWAP), and — under the same
safety caps as smoke — places ONE tiny PAPER long on the MOST-stretched-below-VWAP name whose probability
clears the threshold and that is not already held. The exit is time-based (the reversion horizon). This is
NOT an edge claim (vwap_dev is REAL-but-uneconomic at minute turnover; see docs/EXPERIMENTS.md) — it is the
proof that the platform runs MULTIPLE independent strategy containers, each with its own schema + caps +
``predict``-driven model, paper-only.

What it does each loop:
  1. ``BusConsumer.poll()`` for its declared liquid symbols; keep the LATEST vector per symbol.
  2. MANAGE + FINALIZE: capture entry fills; when a bet's hold expires, submit the closing sell, capture
     the exit fill, compute realized PnL, move OPEN -> CLOSED.
  3. SCORE + maybe place: rank held-eligible symbols by the model's reversion probability; if the best
     clears the threshold and the safety gate allows, place one notional long on it.
  4. SELF-MAINTAIN: reconcile the bet store against the broker on startup; resume managing open bets.

Risk caps (identical philosophy to smoke, env prefix ``REV_``): kill switch, market-hours-only, cadence,
max concurrent, max total open notional (bounds ACTUAL exposure inclusive of the prospective bet), plus
ONE-bet-per-symbol so it never stacks longs on the same stretched name. All bets PAPER-only and tiny.
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
from alpaca.trading.models import Clock, Order
from alpaca.trading.requests import MarketOrderRequest

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.view import FeatureView
from strategies.lib.reversion_model import VwapReversionModel
from strategies.reversion.bet_store import BetStore
from strategies.reversion.contract import STRATEGY_NAME, contract_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("reversion-strategy")

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "SPY", "AMD", "AMZN", "META", "TSLA"]
COID_PREFIX = "rev_"


def _env_symbols() -> list[str]:
    raw = os.environ.get("REV_SYMBOLS", "").strip()
    if not raw:
        return list(DEFAULT_SYMBOLS)
    return [token.strip().upper() for token in raw.split(",") if token.strip()]


@dataclass(frozen=True)
class ReversionConfig:
    """Runtime configuration + risk caps, all overridable via env (fail-safe defaults)."""

    symbols: list[str]
    bet_interval_sec: int
    notional_usd: float
    hold_sec: int
    max_concurrent: int
    max_total_notional_usd: float
    enabled: bool
    loop_block_ms: int
    vwap_window_m: int
    sensitivity: float
    threshold: float

    @classmethod
    def from_env(cls) -> ReversionConfig:
        return cls(
            symbols=_env_symbols(),
            bet_interval_sec=int(os.environ.get("REV_BET_INTERVAL_SEC", "300")),
            notional_usd=float(os.environ.get("REV_NOTIONAL_USD", "50")),
            hold_sec=int(os.environ.get("REV_HOLD_SEC", "1800")),
            max_concurrent=int(os.environ.get("REV_MAX_CONCURRENT", "3")),
            max_total_notional_usd=float(os.environ.get("REV_MAX_TOTAL_NOTIONAL_USD", "200")),
            enabled=os.environ.get("REV_ENABLED", "1") != "0",
            loop_block_ms=int(os.environ.get("REV_LOOP_BLOCK_MS", "1000")),
            vwap_window_m=int(os.environ.get("REV_VWAP_WINDOW_M", "30")),
            sensitivity=float(os.environ.get("REV_SENSITIVITY", "400")),
            threshold=float(os.environ.get("REV_THRESHOLD", "0.60")),
        )


@dataclass(frozen=True)
class BetGate:
    """The decision: may we place a new bet this cycle (cap/cadence/hours), and why not if not?"""

    allowed: bool
    reason: str


def evaluate_bet_gate(
    config: ReversionConfig,
    now: dt.datetime,
    last_bet_ts: dt.datetime | None,
    open_count: int,
    open_notional: float,
    market_open: bool,
) -> BetGate:
    """Pure gate logic (unit-testable, no I/O): kill switch, market hours, cadence, concurrency cap, and
    total-notional cap (INCLUSIVE of the prospective bet's notional). The per-symbol candidate selection +
    threshold is applied separately (``select_candidate``); this gate is the shared safety envelope."""
    if not config.enabled:
        return BetGate(False, "kill_switch_off")
    if not market_open:
        return BetGate(False, "market_closed")
    if last_bet_ts is not None and (now - last_bet_ts).total_seconds() < config.bet_interval_sec:
        return BetGate(False, "within_cadence")
    if open_count >= config.max_concurrent:
        return BetGate(False, "max_concurrent")
    if open_notional + config.notional_usd > config.max_total_notional_usd:
        return BetGate(False, "max_total_notional")
    return BetGate(True, "ok")


@dataclass(frozen=True)
class Candidate:
    """A scored long candidate: the symbol, its reversion probability, and the raw vwap deviation."""

    symbol: str
    probability: float
    deviation: float


def select_candidate(
    model: VwapReversionModel,
    latest_by_symbol: dict[str, FeatureView],
    threshold: float,
    excluded: set[str],
) -> Candidate | None:
    """Score every symbol's latest vector and return the single best long candidate (highest reversion
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
        deviation = vector.value(model.feature_name)
        if not np.isfinite(deviation):
            continue
        if best is None or prediction.probability > best.probability:
            best = Candidate(symbol=symbol, probability=prediction.probability, deviation=float(deviation))
    return best


def order_filled_qty(order: Order) -> float | None:
    """The actual (fractional) share count an order filled, or None if not yet filled."""
    filled_qty = order.filled_qty
    if filled_qty is None:
        return None
    qty = float(filled_qty)
    return qty if qty > 0 else None


class ReversionStrategy:
    """Owns the consume -> score -> bet -> manage -> finalize loop and the broker/store/bus handles."""

    def __init__(
        self,
        config: ReversionConfig,
        consumer: BusConsumer,
        trading: TradingClient,
        store: BetStore,
        model: VwapReversionModel,
    ) -> None:
        self._config = config
        self._consumer = consumer
        self._trading = trading
        self._store = store
        self._model = model
        self._latest_by_symbol: dict[str, FeatureView] = {}
        self._last_bet_ts: dt.datetime | None = None

    def market_open(self) -> bool:
        clock = cast(Clock, self._trading.get_clock())
        return bool(clock.is_open)

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
            logger.warning("order %s not found at broker (%s)", client_order_id, exc)
            return None

    def consume(self) -> None:
        """Poll the bus and keep the LATEST vector per symbol (the score set for this cycle)."""
        vectors = self._consumer.poll_views(block_ms=self._config.loop_block_ms, count=200)
        if not vectors:
            return
        for vector in vectors:
            self._latest_by_symbol[vector.symbol] = vector
        latest = vectors[-1]
        deviation = latest.value(self._model.feature_name)
        logger.info(
            "SAMPLE %s @ %s | %s=%+.5f (%d vectors this poll, %d symbols tracked)",
            latest.symbol,
            latest.minute.isoformat(),
            self._model.feature_name,
            deviation if np.isfinite(deviation) else float("nan"),
            len(vectors),
            len(self._latest_by_symbol),
        )

    def maybe_place_bet(self) -> None:
        """Score candidates and place one notional long on the most-stretched-below-VWAP qualifying name
        (under the safety gate). Records intent in the store BEFORE the order completes, so a crash
        mid-place still leaves a managed (idempotent) bet."""
        now = dt.datetime.now(dt.timezone.utc)
        gate = evaluate_bet_gate(
            config=self._config,
            now=now,
            last_bet_ts=self._last_bet_ts,
            open_count=self._store.count_open(),
            open_notional=self._store.open_notional(),
            market_open=self.market_open(),
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
        coid = f"{COID_PREFIX}{symbol}_{now.strftime('%Y%m%dT%H%M%S')}"
        hold_until = now + dt.timedelta(seconds=self._config.hold_sec)
        self._store.record_open(symbol, "buy", notional, coid, now, hold_until, candidate.probability)
        order = cast(
            Order,
            self._trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    notional=notional,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=coid,
                )
            ),
        )
        self._last_bet_ts = now
        logger.info(
            "BET placed: BUY $%.2f notional %s p=%.3f dev=%+.5f coid=%s broker_id=%s hold=%ds",
            notional,
            symbol,
            candidate.probability,
            candidate.deviation,
            coid,
            str(order.id),
            self._config.hold_sec,
        )

    def manage_open_bets(self) -> None:
        """Capture fills on still-open entries and finalize any bet past its hold."""
        now = dt.datetime.now(dt.timezone.utc)
        for bet in self._store.list_open():
            entry_order_id = str(bet["entry_order_id"])
            if bet["entry_price"] is None:
                order = self._fetch_order_by_coid(entry_order_id)
                if order is not None and order.filled_avg_price:
                    filled_qty = order_filled_qty(order)
                    if filled_qty is not None:
                        filled_price = float(order.filled_avg_price)
                        self._store.mark_filled(entry_order_id, filled_price, filled_qty)
                        bet["entry_price"] = filled_price
                        bet["qty"] = filled_qty
            hold_until = bet["hold_until"]
            if isinstance(hold_until, dt.datetime) and now >= hold_until:
                self._close_bet(bet)

    def _ensure_entry_filled(self, bet: dict[str, object]) -> tuple[float, float] | None:
        """Return (entry_price, filled_qty) for a bet's open, capturing the fill into the store if it
        landed since we last looked. None if the notional entry has not filled — we must NOT submit the
        close until we know how many (fractional) shares we hold."""
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
        exit_coid = f"{entry_order_id}_exit"
        existing_exit = bet["exit_order_id"]
        if existing_exit is None:
            self._store.mark_closing(entry_order_id, exit_coid)
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
        else:
            exit_coid = str(existing_exit)
        exit_order = self._fetch_order_by_coid(exit_coid)
        if exit_order is None or not exit_order.filled_avg_price:
            logger.info("CLOSE pending fill for %s (coid=%s)", symbol, exit_coid)
            return
        exit_price = float(exit_order.filled_avg_price)
        realized = (exit_price - entry_price_value) * qty
        exit_ts = exit_order.filled_at or dt.datetime.now(dt.timezone.utc)
        self._store.record_close(entry_order_id, exit_ts, exit_price, realized)
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
            "reversion strategy starting: symbols=%s enabled=%s interval=%ds notional=$%.0f hold=%ds "
            "caps[concurrent=%d total_notional=$%.0f] model[vwap_window=%dm sensitivity=%.0f threshold=%.2f]",
            self._config.symbols,
            self._config.enabled,
            self._config.bet_interval_sec,
            self._config.notional_usd,
            self._config.hold_sec,
            self._config.max_concurrent,
            self._config.max_total_notional_usd,
            self._config.vwap_window_m,
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
