"""Parity-harness adversarial tests — prove the harness BITES on known-bad input.

"Until parity fails on a known-bad input, it proves nothing." These feed deliberately-broken
live-vs-backfill pairs and assert the harness flags them: a cell mismatch (tolerance method) and a
same-marginal-but-scrambled-cells case (distributional method, audit #5).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features.compare import diff

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
TIERS = pl.DataFrame({"symbol": ["AAA"], "tier": [1]}, schema={"symbol": pl.String, "tier": pl.Int32})
N = 2000  # >= MIN_PARITY_CELLS


def _frame(feature: str, values: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": ["AAA"] * N, "minute": [BASE + timedelta(minutes=i) for i in range(N)], feature: values}
    )


def test_parity_bites_on_cell_mismatch() -> None:
    live = _frame("ret_1m", [0.01] * N)
    backfill = _frame("ret_1m", [0.01 if i % 7 else 0.05 for i in range(N)])  # ~14% of cells differ
    row = diff(live, backfill, TIERS).filter((pl.col("feature") == "ret_1m") & (pl.col("tier") == 1)).row(0, named=True)
    assert row["score"] < 95.0 and row["passed"] is False  # detected


def test_parity_passes_on_identical() -> None:
    live = _frame("ret_1m", [0.01 + (i % 13) * 0.001 for i in range(N)])
    row = diff(live, live.clone(), TIERS).filter((pl.col("feature") == "ret_1m") & (pl.col("tier") == 1)).row(0, named=True)
    assert row["score"] == 100.0 and row["passed"] is True


def test_distributional_method_catches_within_symbol_shuffle() -> None:
    values = [0.1 + (i % 50) * 0.02 for i in range(N)]
    shuffled = values[:]
    random.Random(3).shuffle(shuffled)  # SAME marginal distribution, cells scrambled
    live = _frame("inter_arrival_cv_1m", values)
    backfill = _frame("inter_arrival_cv_1m", shuffled)
    row = diff(live, backfill, TIERS).filter(
        (pl.col("feature") == "inter_arrival_cv_1m") & (pl.col("tier") == 1)
    ).row(0, named=True)
    assert row["method"] == "distributional"
    assert row["passed"] is False  # same shape, scrambled cells -> NOT trustworthy (was a false pass)
