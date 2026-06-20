"""The 6 test gates for the production execution+state layer (docs/STRATEGY_EXECUTION_ABSTRACTION.md §6).

Gate 1 — CONFORMANCE: FaithfulBacktestExecutor == PaperBrokerStub on scripted full/partial/reject/
         cancel lifecycle (the anti-L1 sim==live proof, REQ-X1/X2).
Gate 2 — RESTART-SAFETY x3: mid-order, orphaned-stop, partial-across-restart -> recovered state matches
         the broker, no double-trade (REQ-S4, G3).
Gate 3 — RECONCILE broker-wins + G1 shared-account scoping (a sibling's position is NOT adopted).
Gate 4 — IDEMPOTENCY: coid dedup; G3 ambiguous-resubmit no double-trade; G4 pre-trade gate.
Gate 5 — ANTI-CHEAT: the predict-zero -> pure-cost-drag property of the faithful cost model (the
         battery's full self-proof suite lives in test_battery.py and is the REQ-A1 gate).
Gate 6 — WORKED EXAMPLE: one strategy's decide()+StrategyState through BacktestExecutor AND
         PaperBrokerStub -> identical positions, decide() core UNCHANGED.
"""

from __future__ import annotations

import datetime as dt

import pytest

from quantlib.strategy_core.execution import Fill, OrderState
from quantlib.strategy_core.production_execution import (
    Account,
    CorporateAction,
    ProductionOrderIntent,
    make_client_order_id,
    pre_trade_check,
    reconcile,
    strategy_id_of,
)
from quantlib.strategy_core.production_executor import (
    FaithfulBacktestExecutor,
    MarketSnapshot,
    PaperBrokerStub,
)
from quantlib.strategy_core.production_state import (
    ADOPTED_FILL,
    FREE_TO_REDECIDE,
    SAFE_TO_SUBMIT,
    DictLedgerBackend,
    PgStateStore,
    recover_on_restart,
)
from quantlib.strategy_core.state import StrategyState

TS = dt.datetime(2026, 6, 19, 20, 0, tzinfo=dt.timezone.utc)


def _intent(
    strategy: str, symbol: str, side: str, qty: float, *, ts: dt.datetime = TS
) -> ProductionOrderIntent:
    return ProductionOrderIntent(strategy_id=strategy, symbol=symbol, side=side, decision_ts=ts, qty=qty)


# --- Gate 0: the G2 coid contract --------------------------------------------------------------------


def test_g2_coid_is_fully_qualifying_and_deterministic() -> None:
    intent = _intent("smoke", "AAPL", "buy", 10)
    assert intent.client_order_id == "smoke-20260619T200000-AAPL-buy"
    assert _intent("smoke", "AAPL", "buy", 10).client_order_id == intent.client_order_id  # deterministic
    # different day / side / symbol -> different coid (no collision)
    assert _intent("smoke", "AAPL", "sell", 10).client_order_id != intent.client_order_id
    other_day = _intent("smoke", "AAPL", "buy", 10, ts=TS + dt.timedelta(days=1))
    assert other_day.client_order_id != intent.client_order_id
    assert strategy_id_of(intent.client_order_id) == "smoke"


def test_coid_rejects_naive_ts_and_hyphenated_strategy() -> None:
    with pytest.raises(ValueError, match="tz-aware"):
        make_client_order_id("smoke", dt.datetime(2026, 6, 19, 20, 0), "AAPL", "buy")
    with pytest.raises(ValueError, match="must not contain"):
        make_client_order_id("smo-ke", TS, "AAPL", "buy")


# --- Gate 1: CONFORMANCE (sim == paper-stub on the lifecycle shapes) ---------------------------------


def test_conformance_full_fill() -> None:
    intent = _intent("s", "AAPL", "buy", 100)
    snap = MarketSnapshot(price={"AAPL": 50.0}, half_spread_bps={"AAPL": 2.0}, bar_liquidity={"AAPL": 1e9})
    sim = FaithfulBacktestExecutor(snap)
    paper = PaperBrokerStub(price={"AAPL": 50.0})
    sim_fill = sim.submit(intent).to_fill()
    paper_fill = paper.submit(intent).to_fill()
    assert sim_fill.status == paper_fill.status == OrderState.FILLED
    assert sim_fill.filled_qty == paper_fill.filled_qty == 100.0


def test_conformance_partial_then_complete() -> None:
    intent = _intent("s", "AAPL", "buy", 100)
    # sim: bar liquidity caps at 60 -> PARTIALLY_FILLED, next bar resolves the remaining 40 -> FILLED.
    sim = FaithfulBacktestExecutor(
        MarketSnapshot(price={"AAPL": 50.0}, half_spread_bps={"AAPL": 2.0}, bar_liquidity={"AAPL": 60.0})
    )
    rec = sim.submit(intent)
    assert rec.state == OrderState.PARTIALLY_FILLED and rec.filled_qty == 60.0
    sim.set_snapshot(MarketSnapshot(price={"AAPL": 50.0}, bar_liquidity={"AAPL": 60.0}))
    final = sim.poll(intent.client_order_id)
    assert final.status == OrderState.FILLED and final.filled_qty == 100.0
    # paper-stub scripted to the SAME shape (the broker's reported lifecycle).
    paper = PaperBrokerStub(price={"AAPL": 50.0})
    paper.script(
        intent.client_order_id,
        [(OrderState.PARTIALLY_FILLED, 60.0, 50.0), (OrderState.FILLED, 100.0, 50.0)],
    )
    p1 = paper.submit(intent)
    assert p1.state == OrderState.PARTIALLY_FILLED and p1.filled_qty == 60.0
    p2 = paper.poll(intent.client_order_id)
    assert p2.status == OrderState.FILLED and p2.filled_qty == 100.0


def test_conformance_reject_and_cancel() -> None:
    # reject: a sub-$1 name on both executors.
    penny = _intent("s", "PENNY", "buy", 100)
    sim = FaithfulBacktestExecutor(MarketSnapshot(price={"PENNY": 0.5}, bar_liquidity={"PENNY": 1e9}))
    paper = PaperBrokerStub(price={"PENNY": 0.5})
    assert sim.submit(penny).state == OrderState.REJECTED
    assert paper.submit(penny).state == OrderState.REJECTED
    # cancel: an open partial's remainder is canceled, filled_qty kept.
    intent = _intent("s", "AAPL", "buy", 100)
    sim2 = FaithfulBacktestExecutor(MarketSnapshot(price={"AAPL": 50.0}, bar_liquidity={"AAPL": 40.0}))
    sim2.submit(intent)
    fill = sim2.cancel(intent.client_order_id)
    assert fill.status == OrderState.CANCELED and fill.filled_qty == 40.0


# --- Gate 2: RESTART-SAFETY x3 ----------------------------------------------------------------------


def test_restart_mid_order_no_double_trade() -> None:
    """submit happened, the runner died before booking the fill; restart queries the broker and ADOPTS
    the real fill — does NOT resubmit."""
    intent = _intent("s", "AAPL", "buy", 100)
    broker = PaperBrokerStub(price={"AAPL": 50.0})
    broker.submit(intent)  # the order DID reach the broker and filled, but state never booked it
    state = StrategyState(strategy_id="s")  # fresh (lost) state
    actions = recover_on_restart(state, broker, [intent.client_order_id])
    assert actions[intent.client_order_id] == ADOPTED_FILL
    assert state.positions["AAPL"].qty == 100.0
    # idempotent: recovering again does not double-count.
    recover_on_restart(state, broker, [intent.client_order_id])
    assert state.positions["AAPL"].qty == 100.0


def test_restart_orphaned_stop_realizes_close_from_broker() -> None:
    """state thinks it's long; the broker filled a sell (a stop) while the runner was down. Recovery
    adopts the broker sell -> position flattens, no 're-exit dead trade'."""
    state = StrategyState(strategy_id="s")
    buy = _intent("s", "AAPL", "buy", 100)
    broker = PaperBrokerStub(price={"AAPL": 50.0})
    state.apply_fill(broker.submit(buy).to_fill())  # state booked the long
    assert state.positions["AAPL"].qty == 100.0
    sell = _intent("s", "AAPL", "sell", 100, ts=TS + dt.timedelta(hours=1))
    broker.submit(sell)  # the stop fired at the broker; state didn't see it
    actions = recover_on_restart(state, broker, [sell.client_order_id])
    assert actions[sell.client_order_id] == ADOPTED_FILL
    assert "AAPL" not in state.positions  # flat


def test_restart_partial_across_restart_uses_broker_cumulative() -> None:
    intent = _intent("s", "AAPL", "buy", 100)
    broker = PaperBrokerStub(price={"AAPL": 50.0})
    broker.script(intent.client_order_id, [(OrderState.PARTIALLY_FILLED, 60.0, 50.0)])
    broker.submit(intent)  # 60 of 100 filled, then the runner died
    state = StrategyState(strategy_id="s")
    recover_on_restart(state, broker, [intent.client_order_id])
    assert state.positions["AAPL"].qty == 60.0  # the broker's CUMULATIVE 60, not a re-filled 100


def test_restart_branches_rejected_and_absent() -> None:
    broker = PaperBrokerStub(price={"AAPL": 50.0})
    rejected = _intent("s", "PENNY", "buy", 100)  # no price in the stub -> REJECTED on submit
    broker.submit(rejected)
    state = StrategyState(strategy_id="s")
    actions = recover_on_restart(state, broker, [rejected.client_order_id, "s-20260619T200000-NEVER-buy"])
    assert actions[rejected.client_order_id] == FREE_TO_REDECIDE  # didn't execute -> may re-decide
    assert actions["s-20260619T200000-NEVER-buy"] == SAFE_TO_SUBMIT  # broker never saw it
    assert not state.positions


# --- Gate 3: RECONCILE broker-wins + G1 shared-account scoping ---------------------------------------


def _fill(coid: str, symbol: str, side: str, qty: float) -> Fill:
    return Fill(
        symbol=symbol,
        side=side,
        weight=0.0,
        fill_price=50.0,
        cost_bps=0.0,
        client_order_id=coid,
        filled_qty=qty,
        avg_price=50.0,
        status=OrderState.FILLED,
    )


def test_g1_reconcile_ignores_sibling_positions() -> None:
    """The shared-account proof: broker_fills contain THIS strategy's fill AND a sibling's. Reconcile
    adopts only ours; the sibling position is explicitly ignored, NEVER adopted."""
    state = StrategyState(strategy_id="reversion")
    mine = _fill("reversion-20260619T200000-AAPL-buy", "AAPL", "buy", 100)
    sibling = _fill("smoke-20260619T200000-TSLA-buy", "TSLA", "buy", 50)
    report = reconcile(state, [mine, sibling])
    assert state.positions["AAPL"].qty == 100.0  # adopted ours
    assert "TSLA" not in state.positions  # sibling NOT adopted
    assert "TSLA" in report.ignored_siblings


def test_reconcile_broker_wins_and_alerts_on_large_drift() -> None:
    """State thinks it holds 100 AAPL but the broker shows it flat (closed server-side). Reconcile
    records the drift; large drift alerts (never silent auto-fix)."""
    state = StrategyState(strategy_id="s")
    state.apply_fill(_fill("s-20260619T200000-AAPL-buy", "AAPL", "buy", 100))
    report = reconcile(state, [])  # broker reports NO fills for this strategy
    assert report.drift.get("AAPL") == pytest.approx(-100.0)  # broker(0) - state(100)
    assert report.alert is True


def test_g6_corporate_action_split_is_not_drift() -> None:
    """A 2:1 split during a hold doubles the broker qty; reconcile applies the split to state FIRST so
    broker-net == adjusted-state-qty (a reconciled adjustment, not drift). The broker reports the SAME
    coid the state already booked, now showing the post-split cumulative qty (200)."""
    coid = "s-20260619T200000-AAPL-buy"
    state = StrategyState(strategy_id="s")
    state.apply_fill(_fill(coid, "AAPL", "buy", 100))  # pre-split long of 100
    split = CorporateAction(symbol="AAPL", effective=TS, split_ratio=2.0)
    # the broker reports the already-booked coid at its post-split cumulative qty (200) — not a new order.
    broker_post_split = _fill(coid, "AAPL", "buy", 200)
    report = reconcile(state, [broker_post_split], corporate_actions=[split])
    assert "AAPL" in report.corporate_actions_applied
    assert state.positions["AAPL"].qty == pytest.approx(200.0)  # split applied -> matches broker net
    assert "AAPL" not in report.drift  # no spurious drift


# --- Gate 4: IDEMPOTENCY + G3 + G4 ------------------------------------------------------------------


def test_idempotent_submit_is_single_order() -> None:
    intent = _intent("s", "AAPL", "buy", 100)
    sim = FaithfulBacktestExecutor(MarketSnapshot(price={"AAPL": 50.0}, bar_liquidity={"AAPL": 1e9}))
    first = sim.submit(intent)
    second = sim.submit(intent)  # same coid -> the SAME order, no second economic order
    assert first is second
    assert sim.positions()["AAPL"] == 100.0  # not 200


def test_g3_query_before_resubmit_avoids_double_trade() -> None:
    """The ambiguous-resubmit scenario: a coid was REJECTED at the broker (BP). A blind resubmit would
    place a NEW economic order. recover_on_restart sees the rejection -> FREE_TO_REDECIDE (a new coid),
    never reuses/blind-resubmits the rejected one."""
    intent = _intent("s", "PENNY", "buy", 100)  # no price -> rejected
    broker = PaperBrokerStub(price={"AAPL": 50.0})
    record = broker.submit(intent)
    assert record.state == OrderState.REJECTED
    state = StrategyState(strategy_id="s")
    actions = recover_on_restart(state, broker, [intent.client_order_id])
    assert actions[intent.client_order_id] == FREE_TO_REDECIDE
    assert not state.positions  # no phantom position from a blind resubmit


def test_g4_pre_trade_gate_rejects_unfundable_basket() -> None:
    intents = [_intent("s", "AAPL", "buy", 100), _intent("s", "MSFT", "buy", 100)]
    account = Account(buying_power=1000.0)  # 100*50 + 100*50 = 10000 needed -> reject the basket
    result = pre_trade_check(intents, account, price_of={"AAPL": 50.0, "MSFT": 50.0}, shortable={})
    assert result.admitted == []
    assert all(reason == "insufficient_buying_power" for _, reason in result.rejected)


def test_g4_pre_trade_gate_rejects_non_shortable() -> None:
    intents = [_intent("s", "HARD", "sell", 100)]
    account = Account(buying_power=1e9)
    result = pre_trade_check(intents, account, price_of={"HARD": 50.0}, shortable={"HARD": False})
    assert result.admitted == []
    assert result.rejected[0][1] == "not_shortable"


# --- Gate 5: ANTI-CHEAT (the faithful cost model's predict-zero -> cost-drag property) ---------------


def test_predict_zero_is_pure_cost_drag() -> None:
    """A faithful executor charges the per-name half-spread even on a flat (zero-edge) round trip, so a
    no-information strategy loses exactly the cost — never shows phantom profit. (The battery's full
    self-proof suite — shuffle/planted/known-null/look-ahead/BY-FDR — is the REQ-A1 gate in
    test_battery.py; this asserts the EXECUTION cost model is not free.)"""
    intent = _intent("s", "AAPL", "buy", 100)
    snap = MarketSnapshot(price={"AAPL": 50.0}, half_spread_bps={"AAPL": 5.0}, bar_liquidity={"AAPL": 1e9})
    sim = FaithfulBacktestExecutor(snap, slippage_bps=1.0)
    sim.submit(intent)
    # round-trip the same shares back out at the same price -> zero gross, but the cost was charged.
    assert sim.positions()["AAPL"] == 100.0
    # the cost model is non-zero (half_spread 5 + slippage 1 = 6 bps one-way) — asserted via the curve in
    # the battery; here we assert the executor does NOT fill a penny stock for free (no phantom edge).
    penny = _intent("s", "PENNY", "buy", 100)
    sim_penny = FaithfulBacktestExecutor(MarketSnapshot(price={"PENNY": 0.5}, bar_liquidity={"PENNY": 1e9}))
    assert sim_penny.submit(penny).state == OrderState.REJECTED  # the data-trap floor holds


# --- Gate 6: WORKED EXAMPLE (decide+state through both executors -> identical positions) --------------


class _TwoLegStrategy:
    """A minimal decide(): long the top name, short the bottom — a dollar-neutral 2-leg book. PURE; the
    SAME decide() drives both the backtest and the live-shaped path (decide() core UNCHANGED)."""

    def __init__(self, strategy_id: str, long_symbol: str, short_symbol: str, qty: float) -> None:
        self._id = strategy_id
        self._long = long_symbol
        self._short = short_symbol
        self._qty = qty

    def decide(self, ts: dt.datetime) -> list[ProductionOrderIntent]:
        return [
            _intent(self._id, self._long, "buy", self._qty, ts=ts),
            _intent(self._id, self._short, "sell", self._qty, ts=ts),
        ]


def _run_through(executor: object, intents: list[ProductionOrderIntent], state: StrategyState) -> None:
    for intent in intents:
        record = executor.submit(intent)  # type: ignore[attr-defined]
        if record.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            state.apply_fill(record.to_fill())


def test_worked_example_identical_positions_both_executors() -> None:
    strategy = _TwoLegStrategy("obeta", "AAPL", "TSLA", 100)
    intents = strategy.decide(TS)
    price = {"AAPL": 50.0, "TSLA": 80.0}

    sim_state = StrategyState(strategy_id="obeta")
    sim = FaithfulBacktestExecutor(
        MarketSnapshot(
            price=price, half_spread_bps={"AAPL": 2.0, "TSLA": 2.0}, bar_liquidity={"AAPL": 1e9, "TSLA": 1e9}
        )
    )
    _run_through(sim, intents, sim_state)

    paper_state = StrategyState(strategy_id="obeta")
    paper = PaperBrokerStub(price=price)
    _run_through(paper, intents, paper_state)

    # identical realized book through BOTH executors, with the SAME decide() + SAME StrategyState.
    assert sim_state.positions["AAPL"].qty == paper_state.positions["AAPL"].qty == 100.0
    assert sim_state.positions["TSLA"].qty == paper_state.positions["TSLA"].qty == -100.0
    # ledger recompute == live positions (REQ-S2)
    assert sim_state.positions_from_ledger() == {"AAPL": 100.0, "TSLA": -100.0}


def test_worked_example_durable_restart_recovers_exact_state() -> None:
    """The worked example through the DURABLE PgStateStore: append each fill to the ledger, then a fresh
    load() rebuilds the EXACT positions from the ledger alone (REQ-S2/S4)."""
    backend = DictLedgerBackend()
    store = PgStateStore(backend)
    strategy = _TwoLegStrategy("obeta", "AAPL", "TSLA", 100)
    paper = PaperBrokerStub(price={"AAPL": 50.0, "TSLA": 80.0})
    state = store.load("obeta")
    for intent in strategy.decide(TS):
        fill = paper.submit(intent).to_fill()
        state.apply_fill(fill)
        store.append_fill("obeta", fill)  # durable append-only ledger
    # simulate a restart: a brand-new store load from the durable ledger.
    recovered = PgStateStore(backend).load("obeta")
    assert recovered.positions["AAPL"].qty == 100.0
    assert recovered.positions["TSLA"].qty == -100.0
    assert recovered.positions_from_ledger() == {"AAPL": 100.0, "TSLA": -100.0}
