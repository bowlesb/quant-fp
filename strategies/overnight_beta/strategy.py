"""The overnight-beta strategy — the certified W11 edge, paper-only, as an auction-slippage MEASUREMENT.

The certified signal (`experiments/2026-06-16-w11-overnight-beta/`): LONG the high-beta quintile, SHORT the
low-beta quintile, held OVERNIGHT (enter at the close auction, exit at the next open auction), monthly
beta-quintile rebalance, on the liquid universe excluding the speculation cohort. The container's PRIMARY
purpose is to measure REAL MOO/MOC auction slippage vs the backtest's 5 bps model — the one remaining unknown
gating the +28-30 bps/day overnight net.

Daily loop (driven by the broker clock):
  - NEAR THE CLOSE on a rebalance day: compute beta quintiles from a trailing daily-return panel, submit a
    NOTIONAL market-on-close (Alpaca ``TimeInForce.CLS``) BUY for each high-beta name and SELL for each
    low-beta name, sized to ``OBETA_NOTIONAL_USD`` per leg-name (dollar-neutral). Record the leg + the model
    reference close.
  - AT/AFTER THE OPEN: for each entered leg, submit a market-on-open (``TimeInForce.OPG``) order to FLATTEN,
    capture both auction fills, compute realized PnL, and LOG the realized slippage (fill vs the official
    close/open print) to ``slippage_log``.

Safety (all enforced before any order): kill switch (``OBETA_ENABLED=0`` default → compute + log only, place
NOTHING), market-hours gating, max names per leg, max total gross notional, paper-only. Transient
bus/broker/db errors are caught specifically; bare ``except`` is never used.
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
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide
from alpaca.trading.models import Clock, Order

from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.paper_alpaca_executor import PaperAlpacaExecutor
from quantlib.strategy_core.production_execution import ProductionOrderIntent, make_client_order_id
from quantlib.strategy_core.production_state import PgStateStore
from strategies.lib.overnight_beta_model import OvernightBetaModel
from strategies.lib.stale_entry import StaleEntryTracker, is_order_not_found
from strategies.overnight_beta.contract import STRATEGY_NAME
from strategies.overnight_beta.position_store import PositionStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("overnight-beta-strategy")

# The certification's speculation cohort — excluded by default (the confound control).
DEFAULT_EXCLUDE = (
    "COIN MARA HOOD IONQ CLSK AFRM ASTS APP BBAI CEG GEV CCJ APLD CIFR MSTR NRG OKLO "
    "PLTR QBTS QUBT RGTI RIOT RKLB SMCI SOFI VST WULF"
).split()
COID_PREFIX = "obeta_"


@dataclass(frozen=True)
class OvernightBetaConfig:
    """Runtime config + risk caps, env-driven (fail-safe defaults). PAPER ONLY."""

    notional_usd: float  # $ per leg-name (dollar-neutral)
    max_names_per_leg: int
    max_gross_notional_usd: float
    rebalance_days: int  # rebalance the beta quintiles every N trading days (~21 = monthly)
    beta_window: int  # trailing days for the beta OLS (60 = certified)
    quantile: float  # leg fraction (0.2 = quintile, certified)
    enabled: bool
    exclude: tuple[str, ...]
    loop_sleep_sec: int

    @classmethod
    def from_env(cls) -> OvernightBetaConfig:
        exclude_env = os.environ.get("OBETA_EXCLUDE", "").strip()
        exclude = tuple(s.strip().upper() for s in exclude_env.split(",") if s.strip()) or tuple(
            DEFAULT_EXCLUDE
        )
        return cls(
            notional_usd=float(os.environ.get("OBETA_NOTIONAL_USD", "100")),
            max_names_per_leg=int(os.environ.get("OBETA_MAX_NAMES_PER_LEG", "20")),
            max_gross_notional_usd=float(os.environ.get("OBETA_MAX_GROSS_NOTIONAL_USD", "5000")),
            rebalance_days=int(os.environ.get("OBETA_REBALANCE_DAYS", "21")),
            beta_window=int(os.environ.get("OBETA_BETA_WINDOW", "60")),
            quantile=float(os.environ.get("OBETA_QUANTILE", "0.2")),
            enabled=os.environ.get("OBETA_ENABLED", "0") != "0",  # OFF by default until reviewed
            exclude=exclude,
            loop_sleep_sec=int(os.environ.get("OBETA_LOOP_SLEEP_SEC", "60")),
        )


@dataclass(frozen=True)
class EnterGate:
    allowed: bool
    reason: str


def evaluate_enter_gate(
    config: OvernightBetaConfig,
    market_open: bool,
    minutes_to_close: float | None,
    n_entered: int,
    gross_notional: float,
    prospective_names: int,
) -> EnterGate:
    """Pure gate (unit-testable, no I/O): kill switch, market-hours, the close-auction window, and the
    name/gross caps INCLUSIVE of the prospective entry. We only submit MOC orders in the last few minutes
    before the close (``minutes_to_close`` in (0, 15]); outside that window we wait."""
    if not config.enabled:
        return EnterGate(False, "kill_switch_off")
    if not market_open:
        return EnterGate(False, "market_closed")
    if minutes_to_close is None or not (0.0 < minutes_to_close <= 15.0):
        return EnterGate(False, "not_close_auction_window")
    if n_entered > 0:
        return EnterGate(False, "already_entered_this_overnight")
    prospective_gross = gross_notional + prospective_names * config.notional_usd
    if prospective_gross > config.max_gross_notional_usd:
        return EnterGate(False, "max_gross_notional")
    return EnterGate(True, "ok")


def order_filled(order: Order) -> tuple[float, float] | None:
    """(avg_fill_price, filled_qty) once an order has filled, else None."""
    price = order.filled_avg_price
    qty = order.filled_qty
    if price is None or qty is None:
        return None
    fqty = float(qty)
    return (float(price), fqty) if fqty > 0 else None


class OvernightBetaStrategy:
    """Owns the close-auction-enter / open-auction-flatten loop + the slippage measurement."""

    def __init__(
        self,
        config: OvernightBetaConfig,
        trading: TradingClient,
        store: PositionStore,
        model: OvernightBetaModel,
        panel_loader: "PanelLoader",
        state_store: PgStateStore | None = None,
    ) -> None:
        self._config = config
        self._trading = trading
        self._store = store
        # the production execution+state layer, additive ALONGSIDE the retained bespoke position-store +
        # slippage_log (the same pattern proven live on smoke #220 / reversion #222): broker calls go
        # through PaperAlpacaExecutor (G2 coid) and every captured auction fill is mirrored into the durable
        # StrategyState ledger (the SoT migration). The slippage_log deliverable is untouched.
        self._executor = PaperAlpacaExecutor(trading)
        self._state_store = state_store
        self._state = state_store.load(STRATEGY_NAME) if state_store is not None else None
        self._model = model
        self._panel = panel_loader
        self._last_rebalance: dt.date | None = None
        # per-UTC-date cache of the computed legs so the expensive trailing-panel OLS runs once per day,
        # not every 60s cycle through the close-auction window (the G5 hot-spin).
        self._legs_cache: tuple[tuple[str, ...], tuple[str, ...], dict[str, float]] | None = None
        self._legs_cache_date: dt.date | None = None
        # bounded detector for legs whose close-auction entry never landed (genuinely not-found): stops
        # the every-tick broker re-query spin without ever expiring a live/in-flight order.
        self._stale_entries = StaleEntryTracker()

    def _book_fill(self, symbol: str, side: str, coid: str, qty: float, price: float) -> None:
        """Mirror a captured auction fill into the durable StrategyState ledger (the migration SoT). A
        no-op when no state store is wired, so the change is additive and the decisions are unchanged."""
        if self._state is None or self._state_store is None:
            return
        if any(existing.client_order_id == coid for existing in self._state.fills):
            return  # idempotent on coid (restart-safe; never double-counts a leg)
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

    def _clock(self) -> Clock:
        return cast(Clock, self._trading.get_clock())

    def minutes_to_close(self, clock: Clock) -> float | None:
        if not clock.is_open or clock.next_close is None:
            return None
        return (clock.next_close - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60.0

    def is_rebalance_day(self, today: dt.date) -> bool:
        """Rebalance every ``rebalance_days`` trading days. First run always rebalances; thereafter gate on
        the trading-day gap (approximated by calendar days // rebalance cadence — the broker calendar is the
        source of truth, but a simple gap is adequate for a monthly cadence + the idempotent store)."""
        if self._last_rebalance is None:
            return True
        return (today - self._last_rebalance).days >= self._config.rebalance_days

    def maybe_enter_overnight(self) -> None:
        """On a rebalance day, in the close-auction window, submit the beta-quintile L/S via MOC (CLS).

        The cheap gate (kill switch, market hours, close-auction window, already-entered) is evaluated
        BEFORE the expensive trailing-panel load, so the loop is idle (no parquet fan-out) outside the
        ~15-min close window / when disabled — instead of recomputing the 300-name beta panel every cycle
        with nothing to do (the G5 hot-spin)."""
        clock = self._clock()
        mtc = self.minutes_to_close(clock)
        today = dt.datetime.now(dt.timezone.utc).date()
        if not self.is_rebalance_day(today):
            return
        # cheap pre-gate (prospective_names=0): the kill-switch / market-hours / close-window / already-
        # entered reasons don't depend on the leg count, so this short-circuits the panel load when we
        # could not possibly trade this cycle. The full gross-notional cap is re-checked below with real n.
        pre_gate = evaluate_enter_gate(
            self._config,
            bool(clock.is_open),
            mtc,
            self._store.count_entered(),
            self._gross_notional_estimate(),
            prospective_names=0,
        )
        if not pre_gate.allowed:
            logger.debug("no enter: %s", pre_gate.reason)
            return
        legs = self._select_legs()
        if legs is None:
            return
        long_names, short_names, betas = legs
        n = len(long_names) + len(short_names)
        gate = evaluate_enter_gate(
            self._config,
            bool(clock.is_open),
            mtc,
            self._store.count_entered(),
            self._gross_notional_estimate(),
            n,
        )
        if not gate.allowed:
            logger.debug("no enter: %s", gate.reason)
            return
        ts = dt.datetime.now(dt.timezone.utc)
        for leg, names, side in (
            ("long", long_names, OrderSide.BUY),
            ("short", short_names, OrderSide.SELL),
        ):
            for symbol in names:
                self._submit_close_auction(today, symbol, leg, betas.get(symbol, float("nan")), side, ts)
        self._last_rebalance = today
        logger.info(
            "ENTERED overnight beta L/S: %d long / %d short via CLS auction",
            len(long_names),
            len(short_names),
        )

    def _select_legs(self) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, float]] | None:
        """Compute the beta-quintile legs from the trailing daily-return panel, CACHED per UTC date: the
        panel is daily-return based so the legs are invariant within a day, and the close-auction window
        spans many 60s cycles — recomputing the 300-name OLS each cycle was pure waste (G5). The cache is
        keyed on the date so a new session recomputes; ``None`` (insufficient panel) is not cached so a
        later-arriving panel is retried."""
        today = dt.datetime.now(dt.timezone.utc).date()
        if self._legs_cache is not None and self._legs_cache_date == today:
            return self._legs_cache
        returns_by_name, market_returns = self._panel.load()
        for sym in self._config.exclude:
            returns_by_name.pop(sym, None)
        legs = self._model.select_legs(returns_by_name, market_returns)
        if not legs.long or not legs.short:
            logger.info("no legs (insufficient panel)")
            return None
        cap = self._config.max_names_per_leg
        selected = (legs.long[-cap:], legs.short[:cap], legs.betas)
        self._legs_cache = selected
        self._legs_cache_date = today
        return selected

    def _gross_notional_estimate(self) -> float:
        return self._store.count_entered() * self._config.notional_usd

    def _submit_close_auction(
        self, today: dt.date, symbol: str, leg: str, beta: float, side: OrderSide, ts: dt.datetime
    ) -> None:
        order_side = "buy" if side == OrderSide.BUY else "sell"
        coid = make_client_order_id(STRATEGY_NAME, ts, symbol, order_side)
        ref = self._panel.last_close(symbol)
        target = self._config.notional_usd if side == OrderSide.BUY else -self._config.notional_usd
        self._store.record_enter(today, symbol, leg, beta, target, coid, ts, ref if ref is not None else 0.0)
        intent = ProductionOrderIntent(
            strategy_id=STRATEGY_NAME,
            symbol=symbol,
            side=order_side,
            decision_ts=ts,
            notional=self._config.notional_usd,
            tif="cls",
        )
        record = self._executor.submit(intent)
        if record.state == OrderState.REJECTED:
            logger.warning("CLS submit rejected for %s (coid=%s)", symbol, coid)

    def manage_and_flatten(self) -> None:
        """Capture close-auction fills, then at/after the open submit the OPG flatten + log slippage."""
        clock = self._clock()
        now = dt.datetime.now(dt.timezone.utc)
        for pos in self._store.list_entered():
            enter_coid = str(pos["enter_order_id"])
            symbol = str(pos["symbol"])
            enter_order, not_found = self._fetch_with_status(enter_coid)
            if enter_order is None:
                # a still-unfilled entry that's GENUINELY not-found (the leg never landed) is abandoned
                # after a bounded streak; a transient miss just re-checks next cycle.
                if not_found and pos["enter_fill_price"] is None:
                    self._abandon_if_entry_dead(enter_coid, symbol, now)
                continue
            self._stale_entries.reset(enter_coid)  # found -> clear any not-found streak
            filled = order_filled(enter_order)
            if filled is not None and pos["enter_fill_price"] is None:
                fill_price, qty = filled
                self._store.mark_entered_fill(enter_coid, fill_price, qty)
                ref = float(cast(float, pos["enter_ref_price"]))
                side = "buy" if str(pos["leg"]) == "long" else "sell"
                self._store.log_slippage(symbol, "close", side, ref, fill_price, enter_coid)
                self._book_fill(symbol, side, enter_coid, qty, fill_price)
            # Flatten at the OPEN auction once the market is open (the OPG window).
            if bool(clock.is_open) and pos["exit_order_id"] is None and pos["enter_fill_price"] is not None:
                self._submit_open_auction_flatten(pos)
            elif pos["exit_order_id"] is not None:
                self._capture_flatten(pos)

    def _submit_open_auction_flatten(self, pos: dict[str, object]) -> None:
        symbol = str(pos["symbol"])
        enter_coid = str(pos["enter_order_id"])
        qty = float(cast(float, pos["enter_qty"]))
        # Flatten: a long leg sells, a short leg buys back.
        flatten_side = OrderSide.SELL if str(pos["leg"]) == "long" else OrderSide.BUY
        flatten_side_str = "sell" if flatten_side == OrderSide.SELL else "buy"
        flatten_ts = dt.datetime.now(dt.timezone.utc)
        # the flatten is its OWN G2 coid (the opposite side, the open-auction ts); the store keys the exit
        # on it and reads it back, and the executor submits under it (idempotent, attributable to us, G1).
        intent = ProductionOrderIntent(
            strategy_id=STRATEGY_NAME,
            symbol=symbol,
            side=flatten_side_str,
            decision_ts=flatten_ts,
            qty=qty,
            tif="opg",
        )
        exit_coid = intent.client_order_id
        ref = self._panel.last_open(symbol)
        self._store.mark_exit_submitted(enter_coid, exit_coid, ref if ref is not None else 0.0)
        record = self._executor.submit(intent)
        if record.state == OrderState.REJECTED:
            logger.warning("OPG flatten rejected for %s (coid=%s)", symbol, exit_coid)

    def _capture_flatten(self, pos: dict[str, object]) -> None:
        exit_coid = str(pos["exit_order_id"])
        exit_order = self._fetch(exit_coid)
        if exit_order is None:
            return
        filled = order_filled(exit_order)
        if filled is None:
            return
        exit_fill, _ = filled
        symbol = str(pos["symbol"])
        enter_fill = float(cast(float, pos["enter_fill_price"]))
        qty = float(cast(float, pos["enter_qty"]))
        sign = 1.0 if str(pos["leg"]) == "long" else -1.0
        realized = sign * (exit_fill - enter_fill) * qty
        side = "sell" if str(pos["leg"]) == "long" else "buy"
        ref = float(cast(float, pos["exit_ref_price"]))
        self._store.log_slippage(symbol, "open", side, ref, exit_fill, exit_coid)
        self._store.record_flatten(
            str(pos["enter_order_id"]), dt.datetime.now(dt.timezone.utc), exit_fill, realized
        )
        self._book_fill(symbol, side, exit_coid, qty, exit_fill)
        logger.info(
            "FLATTENED %s %s: enter=%.4f exit=%.4f pnl=%+.4f | mean slippage %s",
            str(pos["leg"]),
            symbol,
            enter_fill,
            exit_fill,
            realized,
            self._store.mean_slippage_bps(),
        )

    def _fetch(self, coid: str) -> Order | None:
        try:
            return cast(Order, self._trading.get_order_by_client_id(coid))
        except APIError:
            return None

    def _fetch_with_status(self, coid: str) -> tuple[Order | None, bool]:
        """Look up an order, distinguishing GENUINELY not-found (40410000) from a transient error. Returns
        ``(order, genuinely_not_found)`` — only a genuine not-found feeds the stale-entry tracker."""
        try:
            return cast(Order, self._trading.get_order_by_client_id(coid)), False
        except APIError as exc:
            if is_order_not_found(exc):
                return None, True
            logger.warning("enter lookup transient error for %s (%s)", coid, exc)
            return None, False

    def _abandon_if_entry_dead(self, enter_coid: str, symbol: str, now: dt.datetime) -> bool:
        """Terminate a leg whose unfilled close-auction entry is GENUINELY not-found for a bounded streak
        (N consecutive over >= M seconds) and stop re-querying it. Returns True iff abandoned this cycle."""
        if self._stale_entries.record_not_found(enter_coid, now):
            logger.warning(
                "ABANDON: enter %s (%s) genuinely not-found x%d — never landed; marking leg terminal",
                enter_coid,
                symbol,
                self._stale_entries.streak_count(enter_coid),
            )
            self._store.mark_abandoned(enter_coid)
            self._stale_entries.forget(enter_coid)
            return True
        return False

    def cycle(self) -> None:
        self.manage_and_flatten()
        self.maybe_enter_overnight()

    def run(self) -> None:
        logger.info(
            "overnight-beta starting: enabled=%s notional=$%.0f/leg max_names=%d max_gross=$%.0f "
            "rebalance=%dd beta_window=%d quantile=%.2f excluded=%d names",
            self._config.enabled,
            self._config.notional_usd,
            self._config.max_names_per_leg,
            self._config.max_gross_notional_usd,
            self._config.rebalance_days,
            self._config.beta_window,
            self._config.quantile,
            len(self._config.exclude),
        )
        while True:
            try:
                self.cycle()
            except APIError as exc:
                logger.warning("broker error (continuing): %s", exc)
            except psycopg.Error as exc:
                logger.warning("db error (continuing): %s", exc)
            time.sleep(self._config.loop_sleep_sec)


class PanelLoader:
    """Loads the trailing daily-return panel for beta estimation. Pluggable so the strategy is testable; the
    live impl reads recent daily bars from the feature store / raw bars. Kept minimal here — the live wiring
    is in __main__; tests inject a fake."""

    def load(self) -> tuple[dict[str, np.ndarray], np.ndarray]:
        raise NotImplementedError

    def last_close(self, symbol: str) -> float | None:
        raise NotImplementedError

    def last_open(self, symbol: str) -> float | None:
        raise NotImplementedError
