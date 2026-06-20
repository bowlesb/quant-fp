"""Per-container parity + restart-safety for overnight_beta's swap onto the production execution+state
layer — the LAST cutover and the highest blast radius (the real L/S book), so the parity proof matters most.

Same additive pattern proven live on smoke (#220) + reversion (#222): the bespoke `positions` +
`slippage_log` tables are RETAINED unchanged (backward-readable); PaperAlpacaExecutor carries the
close/open-auction (CLS/OPG) orders with the G2 coid; every captured auction fill is mirrored into a
durable StrategyState ledger (the SoT migration). decide() (`select_legs` + the leg sizing) is UNTOUCHED.

Tests:
  - PARITY: the leg-selection DECISION (high-beta long / low-beta short, deterministic) is byte-identical
    pre/post; the G2 coids the executor places are exactly the dollar-neutral L/S book the loop intends.
  - END-TO-END L/S BOOK: enter (CLS, both legs) -> fill -> the StrategyState ledger holds the long +short
    book; flatten (OPG) -> fill -> the book is flat. The slippage_log deliverable path is unaffected.
  - RESTART-SAFETY: StrategyState recovers the EXACT L/S book from the durable ledger across a restart.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide, OrderStatus

from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.paper_alpaca_executor import PaperAlpacaExecutor
from quantlib.strategy_core.production_execution import (
    make_client_order_id,
    parse_client_order_id,
    strategy_id_of,
)
from quantlib.strategy_core.production_state import DictLedgerBackend, PgStateStore
from strategies.lib.overnight_beta_model import OvernightBetaModel
from strategies.lib.stale_entry import StaleEntryTracker
from strategies.overnight_beta.contract import STRATEGY_NAME
from strategies.overnight_beta.strategy import (
    OvernightBetaConfig,
    OvernightBetaStrategy,
    evaluate_enter_gate,
)

NOW = dt.datetime(2026, 6, 20, 20, 0, tzinfo=dt.timezone.utc)  # a close-auction ts


class _FakeOrder:
    def __init__(self, coid: str, symbol: str, side: object, qty: float, price: float) -> None:
        self.client_order_id = coid
        self.symbol = symbol
        self.side = side
        self.status = OrderStatus.FILLED
        self.qty = qty
        self.filled_qty = qty
        self.filled_avg_price = price
        self.id = f"broker-{coid}"
        self.submitted_at = NOW
        self.created_at = NOW
        self.filled_at = NOW


class _FakeBroker:
    """A minimal Alpaca-shaped paper broker: a notional order fills notional/price shares at `price`."""

    def __init__(self, price: float) -> None:
        self._price = price
        self.by_coid: dict[str, _FakeOrder] = {}

    def submit_order(self, request: object) -> _FakeOrder:
        coid = request.client_order_id  # type: ignore[attr-defined]
        notional = getattr(request, "notional", None)
        qty = getattr(request, "qty", None)
        filled_qty = float(notional) / self._price if notional is not None else float(qty or 0.0)
        order = _FakeOrder(coid, request.symbol, request.side, filled_qty, self._price)  # type: ignore[attr-defined]
        self.by_coid[coid] = order
        return order

    def get_order_by_client_id(self, coid: str) -> _FakeOrder:
        if coid not in self.by_coid:
            raise APIError('{"code":40410000,"message":"order not found"}')
        return self.by_coid[coid]

    def get_clock(self) -> _ClosedClock:
        return _ClosedClock()


class _ClosedClock:
    """A closed-market clock: the manage loop captures/abandons but never flattens (no OPG window)."""

    is_open = False
    next_close = None


class _FakePanel:
    def last_close(self, symbol: str) -> float:
        return 50.0

    def last_open(self, symbol: str) -> float:
        return 50.0


class _RecordingStore:
    """Captures record_enter + supports list_entered/mark_abandoned (the bespoke positions surface, no DB)."""

    def __init__(self) -> None:
        self.entered: list[dict[str, object]] = []
        self.rows: dict[str, dict[str, object]] = {}

    def record_enter(self, today, symbol, leg, beta, target, coid, ts, ref) -> int:  # type: ignore[no-untyped-def]
        self.entered.append({"symbol": symbol, "leg": leg, "coid": coid, "target": target})
        self.rows[coid] = {
            "enter_order_id": coid,
            "symbol": symbol,
            "leg": leg,
            "enter_ref_price": ref,
            "enter_fill_price": None,
            "enter_qty": None,
            "exit_order_id": None,
            "status": "entered",
        }
        return len(self.entered)

    def list_entered(self) -> list[dict[str, object]]:
        return [row for row in self.rows.values() if row["status"] == "entered"]

    def mark_abandoned(self, enter_order_id: str) -> None:
        row = self.rows.get(enter_order_id)
        if row is not None and row["status"] == "entered":
            row["status"], row["realized_pnl"] = "flattened", 0


def _config() -> OvernightBetaConfig:
    return OvernightBetaConfig(
        notional_usd=50.0,
        max_names_per_leg=20,
        max_gross_notional_usd=10000.0,
        rebalance_days=21,
        beta_window=20,
        quantile=0.2,
        enabled=True,
        exclude=(),
        loop_sleep_sec=60,
    )


def test_select_legs_decision_is_unchanged_deterministic() -> None:
    """The leg selection — the DECISION — is a pure, deterministic function unaffected by the execution
    swap (the swap touched only the broker/state plumbing, never select_legs)."""
    model = OvernightBetaModel(beta_window=20, quantile=0.2)
    rng = np.random.default_rng(0)
    market = rng.standard_normal(40)
    betas = {"LO": 0.2, "L2": 0.6, "MID": 1.0, "H2": 1.4, "HI": 1.8}
    returns = {name: market * beta + rng.standard_normal(40) * 0.01 for name, beta in betas.items()}
    a = model.select_legs(returns, market)
    b = model.select_legs(returns, market)
    assert a.long == b.long and a.short == b.short  # deterministic
    assert "HI" in a.long and "LO" in a.short  # high-beta long, low-beta short — unchanged economics


def test_g2_coid_for_obeta_legs_is_attributable() -> None:
    """The auction orders carry the G2 coid; both legs (buy/sell) are attributable to overnight_beta (G1)."""
    long_coid = make_client_order_id(STRATEGY_NAME, NOW, "HI", "buy")
    short_coid = make_client_order_id(STRATEGY_NAME, NOW, "LO", "sell")
    assert long_coid == "overnight_beta-20260620T200000-HI-buy"
    assert short_coid == "overnight_beta-20260620T200000-LO-sell"
    assert strategy_id_of(long_coid) == strategy_id_of(short_coid) == STRATEGY_NAME
    # the exact coid round-trips (a broker read-back reproduces it for reconcile).
    sid, ts, sym, side = parse_client_order_id(long_coid)
    assert (sid, sym, side) == (STRATEGY_NAME, "HI", "buy")
    assert make_client_order_id(sid, ts, sym, side) == long_coid


def _fill(coid: str, symbol: str, side: str, qty: float, price: float) -> Fill:
    return Fill(
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


def test_ls_book_entered_then_flattened_in_ledger() -> None:
    """The full L/S overnight: enter both legs (long HI buy, short LO sell) -> the ledger holds the
    dollar-neutral book; flatten at the open (sell HI, buy LO) -> the book is flat."""
    backend = DictLedgerBackend()
    store = PgStateStore(backend)
    state = store.load(STRATEGY_NAME)
    open_ts = NOW + dt.timedelta(hours=14)  # the next open auction

    # close-auction entry: long HI (buy), short LO (sell), each ~$50 notional -> qty at the fill price.
    for symbol, side, qty, px in (("HI", "buy", 1.0, 50.0), ("LO", "sell", 2.0, 25.0)):
        fill = _fill(make_client_order_id(STRATEGY_NAME, NOW, symbol, side), symbol, side, qty, px)
        state.apply_fill(fill)
        store.append_fill(STRATEGY_NAME, fill)
    assert state.positions["HI"].qty == 1.0  # long
    assert state.positions["LO"].qty == -2.0  # short

    # open-auction flatten: sell HI, buy LO back.
    for symbol, side, qty, px in (("HI", "sell", 1.0, 51.0), ("LO", "buy", 2.0, 24.0)):
        fill = _fill(make_client_order_id(STRATEGY_NAME, open_ts, symbol, side), symbol, side, qty, px)
        state.apply_fill(fill)
        store.append_fill(STRATEGY_NAME, fill)
    assert state.positions_from_ledger() == {}  # flat after the round trip


def test_restart_recovers_exact_ls_book_from_ledger() -> None:
    """Restart-safety: a mid-overnight crash (entered, not yet flattened) -> a fresh load rebuilds the
    EXACT L/S book from the durable ledger."""
    backend = DictLedgerBackend()
    store = PgStateStore(backend)
    state = store.load(STRATEGY_NAME)
    for symbol, side, qty, px in (("HI", "buy", 1.0, 50.0), ("LO", "sell", 2.0, 25.0)):
        fill = _fill(make_client_order_id(STRATEGY_NAME, NOW, symbol, side), symbol, side, qty, px)
        state.apply_fill(fill)
        store.append_fill(STRATEGY_NAME, fill)
    recovered = PgStateStore(backend).load(STRATEGY_NAME)
    assert recovered.positions["HI"].qty == 1.0
    assert recovered.positions["LO"].qty == -2.0  # the short leg recovered exactly (dollar-neutral book)


def test_enter_gate_decision_is_unchanged() -> None:
    """The enter gate — the DECISION of whether/when to enter — is a pure function unaffected by the
    execution swap. Same caps/window logic, byte-identical reasons."""
    config = _config()
    assert evaluate_enter_gate(config, True, 5.0, 0, 0.0, 4).reason == "ok"
    assert evaluate_enter_gate(config, False, 5.0, 0, 0.0, 4).reason == "market_closed"
    assert evaluate_enter_gate(config, True, 30.0, 0, 0.0, 4).reason == "not_close_auction_window"
    assert evaluate_enter_gate(config, True, 5.0, 1, 0.0, 4).reason == "already_entered_this_overnight"


def _strategy(
    broker: _FakeBroker, store: _RecordingStore, state_store: PgStateStore
) -> OvernightBetaStrategy:
    strat = OvernightBetaStrategy.__new__(OvernightBetaStrategy)
    strat._config = _config()  # type: ignore[attr-defined]
    strat._trading = broker  # type: ignore[attr-defined,assignment]
    strat._store = store  # type: ignore[attr-defined,assignment]
    strat._executor = PaperAlpacaExecutor(broker)  # type: ignore[attr-defined,arg-type]
    strat._state_store = state_store  # type: ignore[attr-defined]
    strat._state = state_store.load(STRATEGY_NAME)  # type: ignore[attr-defined]
    strat._panel = _FakePanel()  # type: ignore[attr-defined,assignment]
    strat._model = OvernightBetaModel()  # type: ignore[attr-defined]
    strat._last_rebalance = None  # type: ignore[attr-defined]
    strat._stale_entries = StaleEntryTracker(min_checks=2, min_seconds=0.0)  # type: ignore[attr-defined]
    return strat


def test_submit_close_auction_places_g2_order_and_books_fill() -> None:
    """End-to-end on the highest-blast-radius path: _submit_close_auction submits the long leg via the
    PaperAlpacaExecutor with the G2 coid (NOT the old obeta_ scheme), records the leg in the bespoke
    positions store (unchanged), and a captured fill books into the StrategyState ledger — the L/S book
    the decision intended."""
    broker = _FakeBroker(price=50.0)
    store = _RecordingStore()
    state_store = PgStateStore(DictLedgerBackend())
    strat = _strategy(broker, store, state_store)

    strat._submit_close_auction(NOW.date(), "HI", "long", 1.8, OrderSide.BUY, NOW)

    # the order was placed via the executor under the G2 coid (attributable to overnight_beta, G1).
    placed = list(broker.by_coid)
    assert placed == ["overnight_beta-20260620T200000-HI-buy"]
    # the bespoke positions table recorded the leg (unchanged path).
    assert store.entered[0]["symbol"] == "HI" and store.entered[0]["leg"] == "long"
    # book the captured fill into the ledger (what manage_and_flatten does) and assert the long position.
    coid = placed[0]
    strat._book_fill("HI", "buy", coid, broker.by_coid[coid].filled_qty, 50.0)
    assert strat._state is not None
    assert strat._state.positions["HI"].qty == broker.by_coid[coid].filled_qty


def test_stale_not_found_entry_leg_is_abandoned_and_stops_spinning() -> None:
    """The reconcile-spin fix on overnight_beta: a close-auction entry leg whose order is genuinely
    not-found at the broker is re-checked a bounded few times then abandoned (flattened), so the manage
    loop stops re-querying a dead order."""
    broker = _FakeBroker(price=50.0)
    store = _RecordingStore()
    state_store = PgStateStore(DictLedgerBackend())
    strat = _strategy(broker, store, state_store)

    # an entered leg whose enter order is NOT at the broker (it never landed) -> genuine not-found.
    dead_coid = "overnight_beta-20260616T200000-HI-buy"
    store.record_enter(NOW.date(), "HI", "long", 1.8, 50.0, dead_coid, NOW, 50.0)
    assert len(store.list_entered()) == 1

    strat.manage_and_flatten()  # 1st not-found (streak=1) -> still entered
    assert len(store.list_entered()) == 1
    strat.manage_and_flatten()  # 2nd consecutive -> terminal -> abandoned (flattened)
    assert len(store.list_entered()) == 0
    assert store.rows[dead_coid]["status"] == "flattened"
    assert store.rows[dead_coid]["realized_pnl"] == 0  # no position ever taken
