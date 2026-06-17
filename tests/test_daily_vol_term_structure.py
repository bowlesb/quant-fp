"""Unit tests for daily_vol_term_structure — daily short/long realized-vol ratio.

Verifies the ratio is consistent with multi_day's daily_vol_{w}d (same definition), and that a flat
daily series emits NULL (not inf). Parity is covered by tests/test_fp_latest.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

WINDOW = 70  # > 60 so the 60d vol is warm on the last day
BASE_DATE = date(2026, 3, 1)


def _daily(closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(closes),
            "date": [BASE_DATE + timedelta(days=i) for i in range(len(closes))],
            "close": closes,
        }
    )


def _ctx(closes: list[float]) -> BatchContext:
    daily = _daily(closes)
    last_date = BASE_DATE + timedelta(days=len(closes) - 1)
    last_min = datetime(
        last_date.year, last_date.month, last_date.day, 14, 0, tzinfo=timezone.utc
    )
    minute = pl.DataFrame({"symbol": ["AAA"], "minute": [last_min]})
    return BatchContext(frames={"daily": daily, "minute_agg": minute})


def _row(out: pl.DataFrame, sym: str = "AAA") -> dict:
    return out.row(0, named=True)


def test_ratio_matches_multi_day_daily_vol() -> None:
    # a varied daily path -> cross-check daily_vol_term_5_20 vs multi_day's daily_vol_5d/daily_vol_20d.
    rng = np.random.default_rng(3)
    closes = [100.0]
    for i in range(1, WINDOW):
        closes.append(closes[-1] * (1.0 + rng.normal(0, 0.02)))
    ctx = _ctx(closes)
    term = run_group(REGISTRY.get_group("daily_vol_term_structure"), ctx)
    md = run_group(REGISTRY.get_group("multi_day_returns"), ctx)
    dv5 = _row(md)["daily_vol_5d"]
    dv20 = _row(md)["daily_vol_20d"]
    assert _row(term)["daily_vol_term_5_20"] == pytest.approx(dv5 / dv20, rel=1e-6)


def test_flat_series_emits_null() -> None:
    ctx = _ctx([7.0] * WINDOW)  # constant -> daily vol ~0 -> NULL, not inf
    out = run_group(REGISTRY.get_group("daily_vol_term_structure"), ctx)
    assert _row(out)["daily_vol_term_5_20"] is None
    assert _row(out)["daily_vol_term_20_60"] is None


def test_expanding_daily_vol_above_one() -> None:
    # calm for most of the window then a SHORT volatile burst in the final few days, so the 5d vol
    # catches the burst but the 20d vol is mostly diluted by calm -> daily_vol_term_5_20 > 1.
    # (point-in-time as of the prior close: the burst must land in the last ~6 closes to enter the 5d
    # _asof window on the final day; keep it short so 20d stays calm-dominated.)
    rng = np.random.default_rng(5)
    closes = [100.0]
    for i in range(1, WINDOW - 5):
        closes.append(closes[-1] * (1.0 + rng.normal(0, 0.001)))  # calm
    for i in range(WINDOW - 5, WINDOW):
        closes.append(closes[-1] * (1.0 + rng.normal(0, 0.05)))  # short volatile burst
    ctx = _ctx(closes)
    out = run_group(REGISTRY.get_group("daily_vol_term_structure"), ctx)
    assert _row(out)["daily_vol_term_5_20"] > 1.5
