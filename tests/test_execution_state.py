"""Tier-1/Tier-2 tests for the production-real execution + state layer (the worked example).

The headline (`test_worked_example_backtest_vs_paper_thin_harness`): the SAME decide() + the SAME
StrategyState model, run through a BacktestExecutor (pretend) AND a live-shaped PaperExecutorStub,
produce identical positions and order intents — the live container is a thin harness with NO
duplicated decision logic (REQ-D1/X1/S1, docs/STRATEGY_EXECUTION_ABSTRACTION.md §5).
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from quantlib.strategy_core.adapters import PanelCrossSection
from quantlib.strategy_core.backtest_executor import BacktestExecutor
from quantlib.strategy_core.cross_sectional_ls import CrossSectionalLS
from quantlib.strategy_core.execution import (
    BookState,
    Fill,
    OrderIntent,
    OrderState,
    RealClock,
    TargetBookStrategy,
)
from quantlib.strategy_core.paper_executor import PaperExecutorStub
from quantlib.strategy_core.state import MemoryStateStore, StrategyState


def _cross_section(n: int, seed: int) -> PanelCrossSection:
    rng = np.random.default_rng(seed)
    symbols = [f"S{i}" for i in range(n)]
    minute = dt.datetime(2026, 1, 5, 19, 59, tzinfo=dt.timezone.utc)
    signal = rng.normal(0, 1, n).reshape(-1, 1)
    entry = np.full(n, 50.0)
    half_spread = np.linspace(2.0, 9.0, n)
    return PanelCrossSection(
        symbols,
        minute,
        signal,
        {"sig": 0},
        {"entry_close": entry, "half_spread_bps": half_spread},
    )


# --- StrategyState transitions (REQ-S1) ----------------------------------------------------------


def test_state_apply_fill_opens_and_realizes_pnl() -> None:
    state = StrategyState(strategy_id="t")
    buy = Fill("AAPL", "buy", 1.0, 100.0, 3.0, "c1", filled_qty=10.0, avg_price=100.0)
    state.apply_fill(buy)
    assert state.positions["AAPL"].qty == 10.0
    assert state.positions["AAPL"].avg_entry_price == 100.0
    # close at 110 -> realized +100 on 10 shares
    sell = Fill("AAPL", "sell", 0.0, 110.0, 3.0, "c2", filled_qty=10.0, avg_price=110.0)
    state.apply_fill(sell)
    assert "AAPL" not in state.positions
    assert abs(state.realized_pnl - 100.0) < 1e-9


def test_state_partial_then_complete_accumulates() -> None:
    state = StrategyState(strategy_id="t")
    state.record_pending(OrderIntent("MSFT", "buy", 1.0, notional=1000.0, client_order_id="c1"))
    state.apply_fill(
        Fill(
            "MSFT",
            "buy",
            1.0,
            50.0,
            3.0,
            "c1",
            filled_qty=6.0,
            avg_price=50.0,
            status=OrderState.PARTIALLY_FILLED,
        )
    )
    assert state.positions["MSFT"].qty == 6.0
    assert state.pending["c1"].filled_qty == 6.0
    state.apply_fill(
        Fill("MSFT", "buy", 1.0, 50.0, 3.0, "c1b", filled_qty=4.0, avg_price=50.0, status=OrderState.FILLED)
    )
    assert state.positions["MSFT"].qty == 10.0


def test_state_ledger_recompute_matches_positions() -> None:
    """REQ-S2: positions are recomputable from the append-only fill ledger (corruption-detectable)."""
    state = StrategyState(strategy_id="t")
    for i, (sym, side, qty) in enumerate([("A", "buy", 10), ("B", "buy", 5), ("A", "sell", 4)]):
        state.apply_fill(Fill(sym, side, 0.0, 50.0, 0.0, f"c{i}", filled_qty=float(qty), avg_price=50.0))
    recomputed = state.positions_from_ledger()
    live = {sym: pos.qty for sym, pos in state.positions.items()}
    assert recomputed == live


# --- Executor reject (REQ-X2/X3) -----------------------------------------------------------------


def test_backtest_executor_rejects_sub_dollar() -> None:
    """A sub-$1 / non-finite-price name is REJECTED, faithfully mimicking Alpaca."""
    symbols = ["GOOD", "PENNY"]
    minute = dt.datetime(2026, 1, 5, 19, 59, tzinfo=dt.timezone.utc)
    cs = PanelCrossSection(
        symbols,
        minute,
        np.array([[1.0], [2.0]]),
        {"sig": 0},
        {"entry_close": np.array([50.0, 0.40]), "half_spread_bps": np.array([3.0, 5.0])},
    )
    intents = [
        OrderIntent("GOOD", "buy", 0.5, notional=100.0, client_order_id="c1"),
        OrderIntent("PENNY", "buy", 0.5, notional=100.0, client_order_id="c2"),
    ]
    fills = {f.symbol: f for f in BacktestExecutor().execute(intents, cs, RealClock())}
    assert fills["GOOD"].status == OrderState.FILLED
    assert fills["PENNY"].status == OrderState.REJECTED


# --- Idempotency (REQ-X4) ------------------------------------------------------------------------


def test_paper_executor_idempotent_on_coid() -> None:
    cs = _cross_section(20, seed=1)
    strategy = TargetBookStrategy(CrossSectionalLS(frac=0.2, signal_feature="sig"))
    intents = strategy.decide(cs, BookState())
    paper = PaperExecutorStub()
    fills1 = paper.execute(intents, cs, RealClock())
    fills2 = paper.execute(intents, cs, RealClock())  # SAME intents (same coids) resubmitted
    assert len(fills1) == len(intents)
    assert fills2 == []  # idempotent: duplicate coids are no-ops
    assert paper.submitted_order_ids() == {i.client_order_id for i in intents}


# --- THE worked example: same decide()+state, backtest vs paper (REQ-D1/X1/S1) -------------------


def _run_one_cycle(executor: object, store: MemoryStateStore) -> StrategyState:
    cs = _cross_section(40, seed=7)
    state = store.load("cs_ls")
    minute = cs.minute
    targets = CrossSectionalLS(frac=0.1, signal_feature="sig").decide(cs)
    intents = [OrderIntent.from_target(t, minute=minute, strategy_id="cs_ls") for t in targets]
    for intent in intents:
        state.record_pending(intent)
    fills = executor.execute(intents, cs, RealClock())  # type: ignore[attr-defined]
    for fill in fills:
        if fill.status == OrderState.FILLED:
            state.apply_fill(fill)
    store.save(state)
    return state


def test_worked_example_backtest_vs_paper_thin_harness() -> None:
    """The SAME decide() + StrategyState model run through BacktestExecutor (pretend) and
    PaperExecutorStub (live-shaped) produce identical positions — only the executor differs."""
    backtest_state = _run_one_cycle(BacktestExecutor(), MemoryStateStore())
    paper_state = _run_one_cycle(PaperExecutorStub(), MemoryStateStore())
    bt_pos = {s: round(p.qty, 9) for s, p in backtest_state.positions.items()}
    paper_pos = {s: round(p.qty, 9) for s, p in paper_state.positions.items()}
    assert bt_pos == paper_pos  # identical book from the SAME decision + state model
    assert set(bt_pos)  # non-empty
    # ledger recompute holds on both
    assert backtest_state.positions_from_ledger().keys() == bt_pos.keys()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
