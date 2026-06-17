"""Unit tests for trade_freq_z — z-score of the per-minute trade count vs the trailing baseline.

Hand-built minute_agg with known n_trades locks in the z-score math + the flat-window (std=0) -> null edge.
Live==backfill parity for this ReductionGroup is covered by the shared tests/test_fp_latest.py.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _minute_agg(counts: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(counts),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(counts))],
            "n_trades": counts,
        }
    )


def _row(out: pl.DataFrame, i: int) -> dict:
    return out.filter(pl.col("minute") == BASE + timedelta(minutes=i)).row(0, named=True)


def _run(frame: pl.DataFrame) -> pl.DataFrame:
    return run_group(REGISTRY.get_group("trade_freq_z"), BatchContext(frames={"minute_agg": frame}))


def test_z_score_known_value() -> None:
    # counts 10,10,10,10,40 over a 5m window at m4: mean=20, std(ddof=1) of [10,10,10,10,40]
    counts = [10.0, 10.0, 10.0, 10.0, 40.0]
    out = _run(_minute_agg(counts))
    vals = counts
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    std = math.sqrt(var)
    expected = (40.0 - mean) / std
    assert _row(out, 4)["trade_freq_z_5m"] == pytest.approx(expected)


def test_flat_window_is_null_not_zero() -> None:
    # all-equal counts -> trailing std = 0 -> z undefined -> null (NOT 0)
    out = _run(_minute_agg([5.0, 5.0, 5.0, 5.0]))
    assert _row(out, 3)["trade_freq_z_5m"] is None


def test_first_minute_is_warmup_null() -> None:
    # a single minute -> std needs >=2 points -> null
    out = _run(_minute_agg([7.0]))
    assert _row(out, 0)["trade_freq_z_5m"] is None


def test_positive_burst_positive_z() -> None:
    out = _run(_minute_agg([1.0, 1.0, 1.0, 100.0]))
    assert _row(out, 3)["trade_freq_z_5m"] > 0  # a surge is a positive z


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
