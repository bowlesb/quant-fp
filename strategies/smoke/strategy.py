"""The smoke strategy — the FIRST strategy container. Deliberately trivial alpha (none, really):
its job is to PROVE the operational apparatus end-to-end before we add real edge.

What it does each loop:
  1. ``BusConsumer.poll()`` for its declared liquid symbols; log a "sample" of a couple real features
     so we can eyeball that live data flows (``ret_1m``, ``volume_zscore_5m``).
  2. On a fixed cadence, during market hours, under the risk caps, place ONE tiny PAPER market buy
     (whole shares ~ SMOKE_NOTIONAL_USD) on the most-recently-seen symbol, ``client_order_id`` prefixed
     ``smoke_`` (namespacing for the shared paper account / a future allocation layer).
  3. MANAGE + FINALIZE: when a bet's hold expires, submit the closing sell, capture fills, compute
     realized PnL, move it OPEN -> CLOSED in the bet store.
  4. SELF-MAINTAIN: on startup, reconcile the bet store against the broker and resume managing open
     bets (closing any already past their hold) — so it survives restarts idempotently.

Risk caps (fail-safe, all enforced before any order): max concurrent bets, max total open notional,
a hard kill switch (SMOKE_ENABLED=0 -> consume + log but place NO orders), and market-hours-only.

Transient bus/broker/db errors must NOT crash the loop: we catch the SPECIFIC client exceptions and
continue. We never catch bare Exception.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import os
import time
from dataclasses import dataclass

import psycopg
import redis
from typing import cast

from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Clock, Order
from alpaca.trading.requests import MarketOrderRequest

from quantlib.bus.consumer import BusConsumer
from strategies.smoke.bet_store import BetStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("smoke-strategy")

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "SPY", "AMD"]
SAMPLE_FEATURES = ["ret_1m", "volume_zscore_5m"]
COID_PREFIX = "smoke_"


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


def compute_qty(notional_usd: float, price: float) -> int:
    """Whole-share quantity for a target notional. Always >= 1 (a tiny smoke bet is never zero
    shares); whole shares keep the exit + per-fill PnL clean (no fractional close bookkeeping)."""
    if price <= 0:
        raise ValueError(f"non-positive price {price}")
    return max(1, math.floor(notional_usd / price))


class SmokeStrategy:
    """Owns the consume -> bet -> manage -> finalize loop and the broker/store/bus handles."""

    def __init__(
        self,
        config: SmokeConfig,
        consumer: BusConsumer,
        trading: TradingClient,
        data_client: StockHistoricalDataClient,
        store: BetStore,
    ) -> None:
        self._config = config
        self._consumer = consumer
        self._trading = trading
        self._data = data_client
        self._store = store
        self._last_symbol: str | None = None
        self._last_bet_ts: dt.datetime | None = None

    def market_open(self) -> bool:
        """Broker clock — only trade during regular hours; outside, consume + log only."""
        clock = cast(Clock, self._trading.get_clock())
        return bool(clock.is_open)

    def _latest_price(self, symbol: str) -> float:
        request = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trades = self._data.get_stock_latest_trade(request)
        return float(trades[symbol].price)

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
            if order is not None and order.filled_avg_price and bet["entry_price"] is None:
                self._store.mark_filled(entry_order_id, float(order.filled_avg_price))
            hold_until = bet["hold_until"]
            if isinstance(hold_until, dt.datetime) and now >= hold_until:
                self._close_bet(bet)

    def _fetch_order_by_coid(self, client_order_id: str) -> Order | None:
        try:
            return cast(Order, self._trading.get_order_by_client_id(client_order_id))
        except APIError as exc:
            logger.warning("reconcile: order %s not found at broker (%s)", client_order_id, exc)
            return None

    def consume_and_sample(self) -> None:
        """Poll the bus and log a sample of real features for the latest vector per symbol."""
        vectors = self._consumer.poll(block_ms=self._config.loop_block_ms, count=200)
        if not vectors:
            return
        for vector in vectors:
            self._last_symbol = vector.symbol
        latest = vectors[-1]
        samples = sample_features(latest)
        rendered = " ".join(f"{name}={value:+.5f}" for name, value in samples.items())
        logger.info(
            "SAMPLE %s @ %s | %s (%d vectors this poll)",
            latest.symbol, latest.minute.isoformat(), rendered, len(vectors),
        )

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
        symbol = str(self._last_symbol)
        price = self._latest_price(symbol)
        qty = compute_qty(self._config.notional_usd, price)
        coid = f"{COID_PREFIX}{symbol}_{now.strftime('%Y%m%dT%H%M%S')}"
        hold_until = now + dt.timedelta(seconds=self._config.hold_sec)
        self._store.record_open(symbol, "buy", qty, coid, now, hold_until)
        order = cast(
            Order,
            self._trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=coid,
                )
            ),
        )
        self._last_bet_ts = now
        logger.info(
            "BET placed: BUY %d %s (~$%.0f) coid=%s broker_id=%s hold=%ds",
            qty, symbol, qty * price, coid, str(order.id), self._config.hold_sec,
        )

    def manage_open_bets(self) -> None:
        """Capture fills on still-open entries and finalize any bet past its hold."""
        now = dt.datetime.now(dt.timezone.utc)
        for bet in self._store.list_open():
            entry_order_id = str(bet["entry_order_id"])
            if bet["entry_price"] is None:
                order = self._fetch_order_by_coid(entry_order_id)
                if order is not None and order.filled_avg_price:
                    filled_price = float(order.filled_avg_price)
                    self._store.mark_filled(entry_order_id, filled_price)
                    bet["entry_price"] = filled_price
            hold_until = bet["hold_until"]
            if isinstance(hold_until, dt.datetime) and now >= hold_until:
                self._close_bet(bet)

    def _close_bet(self, bet: dict[str, object]) -> None:
        """Submit the closing sell (idempotent coid), capture the exit fill, compute realized PnL,
        and move the bet to CLOSED. Re-runs safely: a coid collision means the close is already in
        flight, so we just try to read its fill."""
        entry_order_id = str(bet["entry_order_id"])
        symbol = str(bet["symbol"])
        qty = float(cast(float, bet["qty"]))
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
        entry_price = bet["entry_price"]
        if entry_price is None:
            entry_order = self._fetch_order_by_coid(entry_order_id)
            if entry_order is None or not entry_order.filled_avg_price:
                logger.warning("CLOSE: no entry fill for %s; recording exit only", symbol)
                entry_price_value = exit_price
            else:
                entry_price_value = float(entry_order.filled_avg_price)
                self._store.mark_filled(entry_order_id, entry_price_value)
        else:
            entry_price_value = float(cast(float, entry_price))
        realized = (exit_price - entry_price_value) * qty
        exit_ts = exit_order.filled_at or dt.datetime.now(dt.timezone.utc)
        self._store.record_close(entry_order_id, exit_ts, exit_price, realized)
        logger.info(
            "BET closed: %s entry=%.4f exit=%.4f qty=%g pnl=%+.4f",
            symbol, entry_price_value, exit_price, qty, realized,
        )

    def cycle(self) -> None:
        """One full loop iteration: consume+sample, manage existing bets, maybe open a new one."""
        self.consume_and_sample()
        self.manage_open_bets()
        self.maybe_place_bet()

    def run(self) -> None:
        logger.info(
            "smoke strategy starting: symbols=%s enabled=%s interval=%ds notional=$%.0f hold=%ds "
            "caps[concurrent=%d total_notional=$%.0f]",
            self._config.symbols, self._config.enabled, self._config.bet_interval_sec,
            self._config.notional_usd, self._config.hold_sec, self._config.max_concurrent,
            self._config.max_total_notional_usd,
        )
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
