"""Unit + parity tests for the promoted vol-burst features (experiments/2026-06-19-volburst).

Two genuinely-new feature groups:
  - ``realized_range`` (Layer A, minute_agg ReductionGroup) — short-window trailing mean of the
    intra-minute high-low range (the burst study's ``rv3``). Its compute_latest == compute parity is
    auto-guarded by ``tests/test_fp_latest.py`` (it is a ReductionGroup); here we pin the formula AND add
    an explicit degenerate-window live==backfill parity check.
  - ``large_print_burst`` (Layer C, trades frame) — large-print burst relative to the minute's own mean
    print size. Trades-frame groups are skipped by the generic ``test_fp_latest`` (no trades frame in the
    standard test frames), so we pin its math directly and assert the own-minute parity property (the
    default ``compute_latest`` = ``compute`` filtered to the last minute is byte-identical).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _row(out: pl.DataFrame, minute: datetime) -> dict:
    return out.filter(pl.col("minute") == minute).row(0, named=True)


# --- realized_range (rv3) ---


def _ohlc(bars: list[tuple[float, float, float]]) -> pl.DataFrame:
    """bars = list of (high, low, close) on a contiguous one-minute AAA grid."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(bars),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(bars))],
            "high": [b[0] for b in bars],
            "low": [b[1] for b in bars],
            "close": [b[2] for b in bars],
        }
    )


def test_realized_range_trailing_mean() -> None:
    # 3 bars, ranges/close: m0 (102-98)/100=0.04, m1 (105-95)/100=0.10, m2 (101-99)/100=0.02.
    # realized_range_3m at m2 = mean(0.04, 0.10, 0.02) = 0.0533...
    bars = [(102.0, 98.0, 100.0), (105.0, 95.0, 100.0), (101.0, 99.0, 100.0)]
    out = run_group(REGISTRY.get_group("realized_range"), BatchContext(frames={"minute_agg": _ohlc(bars)}))
    r = _row(out, BASE + timedelta(minutes=2))
    assert r["realized_range_3m"] == pytest.approx((0.04 + 0.10 + 0.02) / 3.0)


def test_realized_range_zero_close_excluded() -> None:
    # A zero-close bar contributes NULL (guarded close>0), excluded from the window mean — not inf/nan.
    bars = [(102.0, 98.0, 100.0), (105.0, 95.0, 0.0), (101.0, 99.0, 100.0)]
    out = run_group(REGISTRY.get_group("realized_range"), BatchContext(frames={"minute_agg": _ohlc(bars)}))
    r = _row(out, BASE + timedelta(minutes=2))
    # window over m0,m1(null),m2 → mean of the two finite values 0.04, 0.02
    assert r["realized_range_3m"] == pytest.approx((0.04 + 0.02) / 2.0)


def test_realized_range_latest_parity_constant_window() -> None:
    # Degenerate constant-range window: live compute_latest == backfill compute().last, no divergence.
    bars = [(101.0, 99.0, 100.0) for _ in range(6)]
    ctx = BatchContext(frames={"minute_agg": _ohlc(bars)})
    group = REGISTRY.get_group("realized_range")
    backfill = group.compute(ctx).sort(["symbol", "minute"])
    last_minute = backfill["minute"].max()
    live = group.compute_latest(ctx).sort("symbol")
    bf_last = backfill.filter(pl.col("minute") == last_minute).select(live.columns).sort("symbol")
    assert live.equals(bf_last)


# --- large_print_burst ---


def _trades(rows: list[tuple[datetime, float]]) -> pl.DataFrame:
    """rows = list of (ts, size); price is constant (size buckets don't depend on price)."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(rows),
            "ts": [r[0] for r in rows],
            "price": [100.0] * len(rows),
            "size": [r[1] for r in rows],
        }
    )


def test_large_print_burst_relative_threshold() -> None:
    # Minute 0: sizes [100, 100, 100, 100, 2000]. mean = 480. threshold = 4*480 = 1920.
    #   large prints (>=1920): just the 2000 print -> 1/5 = 0.2 count share.
    #   large volume share = 2000 / 2400 = 0.8333...
    #   max/mean = 2000/480 = 4.1667
    m0 = BASE
    rows = [
        (m0 + timedelta(seconds=1), 100.0),
        (m0 + timedelta(seconds=2), 100.0),
        (m0 + timedelta(seconds=3), 100.0),
        (m0 + timedelta(seconds=4), 100.0),
        (m0 + timedelta(seconds=5), 2000.0),
    ]
    out = run_group(REGISTRY.get_group("large_print_burst"), BatchContext(frames={"trades": _trades(rows)}))
    r = _row(out, m0)
    mean_size = 2400.0 / 5.0
    assert r["large_print_ratio_1m"] == pytest.approx(1.0 / 5.0)
    assert r["large_print_volume_share_1m"] == pytest.approx(2000.0 / 2400.0)
    assert r["max_print_size_ratio_1m"] == pytest.approx(2000.0 / mean_size)


def test_large_print_burst_uniform_minute_no_large() -> None:
    # All prints equal -> mean equals each, nothing reaches 4x -> ratio 0, vol share 0, max/mean == 1.
    m0 = BASE
    rows = [(m0 + timedelta(seconds=i), 100.0) for i in range(1, 5)]
    out = run_group(REGISTRY.get_group("large_print_burst"), BatchContext(frames={"trades": _trades(rows)}))
    r = _row(out, m0)
    assert r["large_print_ratio_1m"] == pytest.approx(0.0)
    assert r["large_print_volume_share_1m"] == pytest.approx(0.0)
    assert r["max_print_size_ratio_1m"] == pytest.approx(1.0)


def test_large_print_burst_own_minute_parity() -> None:
    # Each minute's cell depends ONLY on its own tape -> compute_latest == compute().last (no window).
    m0, m1 = BASE, BASE + timedelta(minutes=1)
    rows = [
        (m0 + timedelta(seconds=1), 100.0),
        (m0 + timedelta(seconds=2), 5000.0),
        (m1 + timedelta(seconds=1), 200.0),
        (m1 + timedelta(seconds=2), 200.0),
    ]
    ctx = BatchContext(frames={"trades": _trades(rows)})
    group = REGISTRY.get_group("large_print_burst")
    backfill = group.compute(ctx).sort(["symbol", "minute"])
    last_minute = backfill["minute"].max()
    live = group.compute_latest(ctx).sort("symbol")
    bf_last = backfill.filter(pl.col("minute") == last_minute).select(live.columns).sort("symbol")
    assert live.equals(bf_last)


def test_large_print_burst_empty_frame() -> None:
    empty = pl.DataFrame(
        schema={"symbol": pl.String, "ts": pl.Datetime("us", "UTC"), "price": pl.Float64, "size": pl.Float64}
    )
    out = run_group(REGISTRY.get_group("large_print_burst"), BatchContext(frames={"trades": empty}))
    assert out.height == 0
    assert set(out.columns) == {
        "symbol",
        "minute",
        "large_print_ratio_1m",
        "large_print_volume_share_1m",
        "max_print_size_ratio_1m",
    }
