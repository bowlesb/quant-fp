"""Production-fidelity repro (BatteryAudit pass 2, 2026-06-20).

Audits the EXECUTED seam against the REAL live executor (services/executor/main.py +
strategies/overnight_beta). Finding captured here as xfail(strict=True):

  F3 — BacktestExecutor.execute() (the per-event REFERENCE path, the one the parity proof rides on)
       returns a Fill and updates the book to the target weight even when the panel has NO finite
       entry price for that name (fill_price=NaN). The live executor never receives a fill report for
       an unfillable name — a real broker rejects / no-fills it, and reconcile() records it as
       unfilled/rejected (services/executor/main.py:364-366). So the BACKTEST book holds a position
       the reconciled LIVE book never would: backtest > live, silently. A faithful BacktestExecutor
       must refuse the fill (skip it / mark it unfilled) exactly as the broker would.

Lower-severity fidelity gaps (reported to the Lead, not asserted here — they are Phase-0 modeling
choices, not code bugs): the cost model assumes 100% fill on every selected name (no partial/unfilled/
easy-to-borrow gate that the live build_basket enforces); frac=0.1 over liquid_1500 books 150
names/side vs the live 3-6 name book; the live decision cores (OvernightBetaModel.select_legs) do not
yet import quantlib.strategy_core, so the shared-core seam is design-proven, not live-wired.

  F4 (separate, contained) — `quantlib.strategy_core.backtest_executor` cannot be imported COLD
       (before quantlib.battery): it imports quantlib.battery.cost, whose package __init__ imports
       quantlib.battery.strategy, which imports backtest_executor → partially-initialized ImportError.
       A layering violation (the "pure core" reaches UP into the battery). Contained: cross_sectional_ls
       / execution / feeds / adapters (what a live container actually imports) all load cold fine; only
       this backtest-only module has the cycle. Hence this test imports quantlib.battery first.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

import quantlib.battery  # noqa: F401  (import the package first — see F4 below re: import cycle)
from quantlib.strategy_core.adapters import PanelCrossSection
from quantlib.strategy_core.backtest_executor import BacktestExecutor
from quantlib.strategy_core.cross_sectional_ls import CrossSectionalLS
from quantlib.strategy_core.execution import BookState, RealClock, TargetBookStrategy


def test_backtest_executor_refuses_unfillable_name() -> None:
    """A name with no finite entry price must NOT produce a Fill / enter the book — the live broker
    would never report a fill for it. Today execute() books it at fill_price=NaN, so the backtest
    book diverges from a reconciled live book (backtest holds a position live cannot)."""
    symbols = ["A", "B"]
    cs = PanelCrossSection(
        symbols,
        dt.datetime(2025, 1, 6, 14, 35, tzinfo=dt.timezone.utc),
        np.array([[2.0], [-2.0]]),
        {"sig": 0},
        {"entry_close": np.array([50.0, np.nan]), "half_spread_bps": np.array([3.0, 3.0])},
    )
    intents = TargetBookStrategy(CrossSectionalLS(frac=0.5, signal_feature="sig")).decide(cs, BookState())
    executor = BacktestExecutor()
    fills = executor.execute(intents, cs, RealClock())

    # a faithful executor must not return a fill at a non-finite price ...
    assert all(np.isfinite(fill.fill_price) for fill in fills), (
        "BacktestExecutor returned a Fill at a NaN price (live broker would reject/no-fill)"
    )
    # ... nor leave that name in the book as if it filled.
    assert "B" not in executor.book().weights or executor.book().weights["B"] == 0.0
