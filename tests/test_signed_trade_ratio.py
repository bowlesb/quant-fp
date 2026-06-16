"""Unit tests for the signed_trade_ratio group — net signed volume / total volume, windowed.

Hand-built minute_agg rows with known signed_volume + volume lock in the ratio math and the
zero-volume -> null edge. Live==backfill parity for this ReductionGroup is covered by the shared
tests/test_fp_latest.py (it auto-discovers every registered group); these tests pin the per-cell values.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _minute_agg(rows: list[tuple[float, float]]) -> pl.DataFrame:
    """rows = list of (signed_volume, volume) on a contiguous one-minute AAA grid."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(rows),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(rows))],
            "signed_volume": [r[0] for r in rows],
            "volume": [r[1] for r in rows],
        }
    )


def _row(out: pl.DataFrame, i: int) -> dict:
    return out.filter(pl.col("minute") == BASE + timedelta(minutes=i)).row(0, named=True)


def _run(frame: pl.DataFrame) -> pl.DataFrame:
    return run_group(REGISTRY.get_group("signed_trade_ratio"), BatchContext(frames={"minute_agg": frame}))


def test_ratio_single_minute_is_signed_over_total() -> None:
    # m0: +300 signed of 1000 total -> 0.30; m1: -400 of 1000 -> -0.40 (1m via the 5m rolling at its start)
    frame = _minute_agg([(300.0, 1000.0)])
    m0 = _row(_run(frame), 0)
    assert m0["signed_trade_ratio_5m"] == pytest.approx(0.30)
    assert m0["signed_trade_ratio_15m"] == pytest.approx(0.30)


def test_ratio_rolls_over_window() -> None:
    # Three minutes: signed 100,200,-150 ; volume 500,400,600. Trailing 5m sum at m2:
    # signed = 100+200-150 = 150 ; total = 500+400+600 = 1500 -> 0.10
    frame = _minute_agg([(100.0, 500.0), (200.0, 400.0), (-150.0, 600.0)])
    out = _run(frame)
    assert _row(out, 2)["signed_trade_ratio_5m"] == pytest.approx(150.0 / 1500.0)
    # at m1 the trailing window is m0+m1: (100+200)/(500+400) = 300/900
    assert _row(out, 1)["signed_trade_ratio_5m"] == pytest.approx(300.0 / 900.0)


def test_ratio_bounded_minus_one_to_one() -> None:
    # all-buy: signed == volume -> ratio +1 ; all-sell: signed == -volume -> ratio -1
    out_buy = _run(_minute_agg([(1000.0, 1000.0)]))
    out_sell = _run(_minute_agg([(-1000.0, 1000.0)]))
    assert _row(out_buy, 0)["signed_trade_ratio_5m"] == pytest.approx(1.0)
    assert _row(out_sell, 0)["signed_trade_ratio_5m"] == pytest.approx(-1.0)


def test_zero_volume_window_is_null_not_zero() -> None:
    # A tradeless minute: signed 0 of volume 0 -> ratio is mathematically undefined -> null (NOT 0.0).
    out = _run(_minute_agg([(0.0, 0.0)]))
    assert _row(out, 0)["signed_trade_ratio_5m"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
