"""Unit tests for liquidity_rank — the persistent cross-sectional liquidity tier.

Hand-built daily panel with KNOWN dollar volumes so the trailing-ADV percentile rank is deterministic.
Parity (compute_latest == compute on the last minute) is covered by tests/test_fp_latest.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import math

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

WINDOW = 25  # > 20 so the 20d ADV is warm on the last day
BASE_DATE = date(2026, 3, 1)


def _ctx() -> BatchContext:
    """3 symbols with constant but DIFFERENT daily dollar volume: LOW < MID < HIGH.
    close=10 for all; volume sets the dollar volume → adv rank LOW=1/3, MID=2/3, HIGH=1.
    """
    specs = {"LOW": 100.0, "MID": 1000.0, "HIGH": 10000.0}  # daily volume
    rows = []
    for sym, vol in specs.items():
        for i in range(WINDOW):
            rows.append(
                {
                    "symbol": sym,
                    "date": BASE_DATE + timedelta(days=i),
                    "close": 10.0,
                    "volume": vol,
                }
            )
    daily = pl.DataFrame(rows)
    last_date = BASE_DATE + timedelta(days=WINDOW - 1)
    last_min = datetime(
        last_date.year, last_date.month, last_date.day, 14, 0, tzinfo=timezone.utc
    )
    minute = pl.DataFrame({"symbol": ["LOW", "MID", "HIGH"], "minute": [last_min] * 3})
    return BatchContext(frames={"daily": daily, "minute_agg": minute})


def _row(out: pl.DataFrame, sym: str) -> dict:
    return out.filter(pl.col("symbol") == sym).row(0, named=True)


def test_rank_orders_by_liquidity() -> None:
    out = run_group(REGISTRY.get_group("liquidity_rank"), _ctx())
    # 3 names → average-rank percentiles 1/3, 2/3, 3/3.
    assert _row(out, "LOW")["liquidity_rank"] == pytest.approx(1 / 3)
    assert _row(out, "MID")["liquidity_rank"] == pytest.approx(2 / 3)
    assert _row(out, "HIGH")["liquidity_rank"] == pytest.approx(1.0)


def test_adv_log_level() -> None:
    out = run_group(REGISTRY.get_group("liquidity_rank"), _ctx())
    # HIGH: close 10 * volume 10000 = 100,000 dollar vol/day → log1p(100000).
    assert _row(out, "HIGH")["adv_dollar_log_20d"] == pytest.approx(
        math.log1p(100000.0)
    )
    assert _row(out, "LOW")["adv_dollar_log_20d"] == pytest.approx(math.log1p(1000.0))


def test_rank_in_unit_interval() -> None:
    out = run_group(REGISTRY.get_group("liquidity_rank"), _ctx())
    ranks = out["liquidity_rank"].drop_nulls()
    assert (ranks >= 0.0).all() and (ranks <= 1.0).all()
