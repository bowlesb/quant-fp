"""Unit tests for vol_term_structure — short/long realized-vol ratio.

Verifies the ratio against the SAME realized_vol the volatility group computes (so the two groups are
consistent), that an expanding-vol window gives vol_term > 1, and that a flat long-window emits NULL
(not inf). Parity (compute_latest == compute on the last minute) is covered by tests/test_fp_latest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc)


def _ctx(closes: list[float]) -> BatchContext:
    # include high/low (= close) so the volatility group (which the consistency test cross-checks) runs.
    rows = [
        {
            "symbol": "AAA",
            "minute": BASE + timedelta(minutes=i),
            "open": float(c),
            "high": float(c),
            "low": float(c),
            "close": float(c),
            "volume": 1000.0,
        }
        for i, c in enumerate(closes)
    ]
    return BatchContext(frames={"minute_agg": pl.DataFrame(rows)})


def _row(out: pl.DataFrame, minute_idx: int) -> dict:
    return out.filter(pl.col("minute") == BASE + timedelta(minutes=minute_idx)).row(
        0, named=True
    )


def test_vol_term_matches_realized_vol_ratio() -> None:
    # a varied 70-minute path -> compute vol_term_5_30 and compare to the volatility group's
    # realized_vol_5m / realized_vol_30m at the last minute (consistency by construction).
    closes = [100.0]
    for i in range(1, 70):
        closes.append(closes[-1] * (1.0 + ((-1) ** i) * 0.002 * (1 + (i % 5))))
    ctx = _ctx(closes)
    vts = run_group(REGISTRY.get_group("vol_term_structure"), ctx)
    vol = run_group(REGISTRY.get_group("volatility"), ctx)
    last = len(closes) - 1
    rv5 = _row(vol, last)["realized_vol_5m"]
    rv30 = _row(vol, last)["realized_vol_30m"]
    expected = rv5 / rv30
    assert _row(vts, last)["vol_term_5_30"] == pytest.approx(expected, rel=1e-6)


def test_expanding_vol_gives_ratio_above_one() -> None:
    # calm for 40 min then a volatile burst in the last 10 -> short-vol >> long-vol -> vol_term_10_60 > 1.
    closes = [100.0]
    for i in range(1, 50):
        closes.append(closes[-1] * (1.0 + 0.0002 * ((-1) ** i)))  # tiny moves (calm)
    for i in range(50, 70):
        closes.append(closes[-1] * (1.0 + 0.01 * ((-1) ** i)))  # big moves (burst)
    ctx = _ctx(closes)
    out = run_group(REGISTRY.get_group("vol_term_structure"), ctx)
    assert (
        _row(out, 69)["vol_term_10_60"] > 1.5
    )  # short-horizon vol much higher than long


def test_flat_window_emits_null_not_inf() -> None:
    # 70 perfectly-flat minutes -> long_vol ~ 0 -> NULL (not +/-inf or NaN).
    ctx = _ctx([5.0] * 70)
    out = run_group(REGISTRY.get_group("vol_term_structure"), ctx)
    row = _row(out, 69)
    assert row["vol_term_10_60"] is None
    assert row["vol_term_5_30"] is None
