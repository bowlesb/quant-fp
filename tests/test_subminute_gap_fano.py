"""Unit tests for subminute_gap_fano — trailing mean of the within-minute inter-trade-gap Fano factor.

Hand-built tick frames with known sub-minute gap timing pin the per-minute Fano + the windowed-mean +
the single-trade (no-gap) -> null edge. The generic test_fp_latest skips trades-frame groups (no trades
frame in the standard test frames), and the live==backfill parity of this compute_latest_on_window group
is exercised additionally below by comparing compute_latest to compute().last directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _row(out: pl.DataFrame, minute: datetime) -> dict:
    return out.filter(pl.col("minute") == minute).row(0, named=True)


def _trades(rows: list[tuple[int, int]]) -> pl.DataFrame:
    """rows = list of (minute_index, microsecond_offset_within_minute) for one symbol."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(rows),
            "ts": [BASE + timedelta(minutes=mi, microseconds=us) for mi, us in rows],
            "price": [100.0] * len(rows),
            "size": [10.0] * len(rows),
        }
    )


def _run(frame: pl.DataFrame) -> pl.DataFrame:
    return run_group(REGISTRY.get_group("subminute_gap_fano"), BatchContext(frames={"trades": frame}))


def _gap_fano(gaps_us: list[float]) -> float:
    mean = sum(gaps_us) / len(gaps_us)
    var = sum((g - mean) ** 2 for g in gaps_us) / (len(gaps_us) - 1)  # ddof=1
    return var / mean


def test_single_minute_gap_fano_known_value() -> None:
    # One minute, 4 trades at 0, 100us, 300us, 600us -> gaps [100, 200, 300] us.
    out = _run(_trades([(0, 0), (0, 100), (0, 300), (0, 600)]))
    expected = _gap_fano([100.0, 200.0, 300.0])
    assert _row(out, BASE)["subminute_gap_fano_60m"] == pytest.approx(expected, rel=1e-9)


def test_windowed_mean_of_two_minutes() -> None:
    # minute 0: gaps [100, 200, 300]; minute 1: gaps [50, 50] (even -> Fano 0).
    trades = _trades([(0, 0), (0, 100), (0, 300), (0, 600), (1, 0), (1, 50), (1, 100)])
    out = _run(trades)
    f0 = _gap_fano([100.0, 200.0, 300.0])
    f1 = _gap_fano([50.0, 50.0])  # == 0
    # At minute 1 the trailing 60m mean averages both minutes' Fano.
    assert _row(out, BASE + timedelta(minutes=1))["subminute_gap_fano_60m"] == pytest.approx(
        (f0 + f1) / 2.0, rel=1e-9
    )


def test_single_trade_minute_is_null() -> None:
    # A minute with one trade has no gap -> per-minute Fano null -> the only minute -> window mean null.
    out = _run(_trades([(0, 0)]))
    assert _row(out, BASE)["subminute_gap_fano_60m"] is None


def test_compute_latest_matches_backfill_last() -> None:
    # compute_latest_on_window must equal compute().last cell-for-cell (parity-true by construction).
    trades = _trades([(0, 0), (0, 100), (0, 300), (1, 0), (1, 50), (1, 400), (2, 0), (2, 90)])
    group = REGISTRY.get_group("subminute_gap_fano")
    ctx = BatchContext(frames={"trades": trades})
    rolling = group.compute(ctx)
    last = rolling["minute"].max()
    expected = rolling.filter(pl.col("minute") == last).row(0, named=True)["subminute_gap_fano_60m"]
    actual = group.compute_latest(ctx).row(0, named=True)["subminute_gap_fano_60m"]
    assert actual == pytest.approx(expected, rel=1e-9)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
