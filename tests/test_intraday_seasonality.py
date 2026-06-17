"""Unit tests for intraday_seasonality — time-of-day-normalized activity.

The group loads the FROZEN committed baseline (data/intraday_seasonality_v1.parquet). These tests
build a known minute panel and verify the normalization IDENTITY against whatever baseline is
committed (absret_vs_tod * baseline_absret[bucket] == raw |ret|), so they are robust to the baseline's
exact values. Parity (compute_latest == compute on the last minute) is covered by tests/test_fp_latest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group
from quantlib.features.groups.intraday_seasonality import _load_baseline

# 13:30 UTC = 09:30 ET (June, EDT) — the open bucket (570).
OPEN = datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc)
_BASELINE = _load_baseline()


def _ctx(opens: list[float], closes: list[float], vols: list[float]) -> BatchContext:
    rows = []
    for i, (op, cl, vol) in enumerate(zip(opens, closes, vols)):
        rows.append(
            {
                "symbol": "AAA",
                "minute": OPEN + timedelta(minutes=i),
                "open": float(op),
                "close": float(cl),
                "volume": float(vol),
            }
        )
    return BatchContext(frames={"minute_agg": pl.DataFrame(rows)})


def _row(out: pl.DataFrame, minute_idx: int) -> dict:
    return out.filter(pl.col("minute") == OPEN + timedelta(minutes=minute_idx)).row(
        0, named=True
    )


def _baseline_at(bucket: int) -> dict:
    return _BASELINE.filter(pl.col("bucket") == bucket).row(0, named=True)


@pytest.mark.skipif(_BASELINE.height == 0, reason="baseline table not committed")
def test_absret_vs_tod_identity() -> None:
    # all minutes in the 09:30 bucket (570). raw |ret| at minute 0: |101/100 - 1| = 0.01.
    ctx = _ctx(
        opens=[100, 100, 100], closes=[101.0, 99.0, 100.0], vols=[1000, 2000, 1500]
    )
    out = run_group(REGISTRY.get_group("intraday_seasonality"), ctx)
    base = _baseline_at(570)["baseline_absret"]
    assert _row(out, 0)["absret_vs_tod"] == pytest.approx(0.01 / base, rel=1e-6)
    assert _row(out, 1)["absret_vs_tod"] == pytest.approx(0.01 / base, rel=1e-6)


@pytest.mark.skipif(_BASELINE.height == 0, reason="baseline table not committed")
def test_volume_vs_tod_uses_running_mean_and_shape() -> None:
    # vols 1000, 3000 → running means 1000, 2000. shape = vol_shape[570].
    ctx = _ctx(opens=[100, 100], closes=[101.0, 101.0], vols=[1000.0, 3000.0])
    out = run_group(REGISTRY.get_group("intraday_seasonality"), ctx)
    shape = _baseline_at(570)["vol_shape"]
    # minute 0: vol 1000 / (running-mean 1000 * shape) = 1/shape.
    assert _row(out, 0)["volume_vs_tod"] == pytest.approx(1.0 / shape, rel=1e-6)
    # minute 1: vol 3000 / (running-mean 2000 * shape) = 1.5/shape.
    assert _row(out, 1)["volume_vs_tod"] == pytest.approx(1.5 / shape, rel=1e-6)


@pytest.mark.skipif(_BASELINE.height == 0, reason="baseline table not committed")
def test_baseline_has_all_rth_buckets() -> None:
    # 13 thirty-min buckets from 570 (09:30) to 930 (15:30) inclusive.
    buckets = set(_BASELINE["bucket"].to_list())
    assert {570, 600, 930}.issubset(buckets)
    assert (_BASELINE["baseline_absret"] > 0).all()
    assert (_BASELINE["vol_shape"] > 0).all()
