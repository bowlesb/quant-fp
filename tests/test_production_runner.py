"""`ProductionRunner` end-to-end wiring: decide -> gate -> submit -> StrategyState + durable ledger ->
manage -> recover/reconcile, through both the faithful sim and the paper stub (the SAME runner, swapped
components — REQ-D1/X1/S2)."""

from __future__ import annotations

import datetime as dt

from quantlib.strategy_core.production_execution import ProductionOrderIntent
from quantlib.strategy_core.production_executor import (
    FaithfulBacktestExecutor,
    MarketSnapshot,
    PaperBrokerStub,
)
from quantlib.strategy_core.production_runner import ProductionRunner
from quantlib.strategy_core.production_state import DictLedgerBackend, PgStateStore
from quantlib.strategy_core.state import MemoryStateStore

TS = dt.datetime(2026, 6, 19, 20, 0, tzinfo=dt.timezone.utc)


def _intents() -> list[ProductionOrderIntent]:
    return [
        ProductionOrderIntent(strategy_id="rev", symbol="AAPL", side="buy", decision_ts=TS, qty=100),
        ProductionOrderIntent(strategy_id="rev", symbol="TSLA", side="sell", decision_ts=TS, qty=100),
    ]


def test_runner_submits_and_books_state_through_faithful_sim() -> None:
    snap = MarketSnapshot(
        price={"AAPL": 50.0, "TSLA": 80.0},
        half_spread_bps={"AAPL": 2.0, "TSLA": 2.0},
        bar_liquidity={"AAPL": 1e9, "TSLA": 1e9},
    )
    runner = ProductionRunner("rev", FaithfulBacktestExecutor(snap), MemoryStateStore())
    submitted = runner.submit_intents(_intents())
    assert len(submitted) == 2
    assert runner.state.positions["AAPL"].qty == 100.0
    assert runner.state.positions["TSLA"].qty == -100.0


def test_runner_durable_ledger_recovers_after_restart() -> None:
    backend = DictLedgerBackend()
    runner = ProductionRunner(
        "rev", PaperBrokerStub(price={"AAPL": 50.0, "TSLA": 80.0}), PgStateStore(backend)
    )
    runner.submit_intents(_intents())
    # a fresh runner on the SAME durable backend recovers the exact state from the ledger.
    restarted = ProductionRunner(
        "rev", PaperBrokerStub(price={"AAPL": 50.0, "TSLA": 80.0}), PgStateStore(backend)
    )
    assert restarted.state.positions["AAPL"].qty == 100.0
    assert restarted.state.positions["TSLA"].qty == -100.0
    assert restarted.state.positions_from_ledger() == {"AAPL": 100.0, "TSLA": -100.0}


def test_runner_manage_books_partial_increment_once() -> None:
    intent = ProductionOrderIntent(strategy_id="rev", symbol="AAPL", side="buy", decision_ts=TS, qty=100)
    sim = FaithfulBacktestExecutor(
        MarketSnapshot(price={"AAPL": 50.0}, bar_liquidity={"AAPL": 60.0})  # partial: 60 of 100
    )
    runner = ProductionRunner("rev", sim, MemoryStateStore())
    runner.submit_intents([intent])
    assert runner.state.positions["AAPL"].qty == 60.0  # the partial
    sim.set_snapshot(MarketSnapshot(price={"AAPL": 50.0}, bar_liquidity={"AAPL": 60.0}))
    runner.manage([intent.client_order_id])  # resolves the remaining 40 -> 100, booked ONCE
    assert runner.state.positions["AAPL"].qty == 100.0
    assert runner.state.positions_from_ledger() == {"AAPL": 100.0}  # no double-count
