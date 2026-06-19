"""Unit + parity tests for the batch-3 promoted features (experiments/2026-06-19-feature-invention).

Three genuinely-new feature groups screened against the validated vol-burst label:
  - ``range_expansion`` (Layer A, minute_agg ReductionGroup) — ratio of the recent-window mean intrabar
    range to the trailing-window mean. Its compute_latest == compute parity is auto-guarded by
    ``tests/test_fp_latest.py`` (it is a ReductionGroup); here we pin the formula AND add an explicit
    degenerate-window live==backfill parity check.
  - ``print_hhi`` (Layer C, trades frame) — trailing-window mean of the within-minute notional
    Herfindahl. A trades-frame WINDOWED group (compute_latest = compute_latest_on_window), skipped by the
    generic test_fp_latest (no trades frame there), so we pin its math + assert the window-slice parity.
  - ``size_entropy`` (Layer C, trades frame, the batch-3 YELLOW) — Shannon entropy of the trailing-window
    trade-size distribution over 6 order-of-magnitude bins. YELLOW => a DEDICATED degenerate-window
    live==backfill parity test (single-scale zero-entropy + constant-count windows) is mandatory.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _row(out: pl.DataFrame, minute: datetime) -> dict:
    return out.filter(pl.col("minute") == minute).row(0, named=True)


def _assert_window_parity(group_name: str, frames: dict[str, pl.DataFrame]) -> None:
    """live compute_latest == backfill compute().last, cell-for-cell (the parity-by-construction check)."""
    ctx = BatchContext(frames=frames)
    group = REGISTRY.get_group(group_name)
    backfill = group.compute(ctx).sort(["symbol", "minute"])
    last_minute = backfill["minute"].max()
    live = group.compute_latest(ctx).sort("symbol")
    bf_last = backfill.filter(pl.col("minute") == last_minute).select(live.columns).sort("symbol")
    assert live.equals(bf_last)


# --- range_expansion (Layer A, minute_agg) ---


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


def test_range_expansion_ratio() -> None:
    # 6 contiguous bars, range/close per minute (close=100 throughout):
    #   m0 .02, m1 .02, m2 .02, m3 .02, m4 .02, m5 .10  (range = high-low).
    # range_expansion_5_30m at m5 = mean(recent 5m: m1..m5) / mean(trailing 30m: m0..m5).
    #   recent 5m (m1..m5) = mean(.02,.02,.02,.02,.10) = .036
    #   trailing 30m (m0..m5) = mean(.02 x5, .10) = (0.10 + 0.10)/6 = 0.0333...
    bars = [(101.0, 99.0, 100.0)] * 5 + [(105.0, 95.0, 100.0)]
    out = run_group(REGISTRY.get_group("range_expansion"), BatchContext(frames={"minute_agg": _ohlc(bars)}))
    r = _row(out, BASE + timedelta(minutes=5))
    recent = (0.02 + 0.02 + 0.02 + 0.02 + 0.10) / 5.0
    trailing = (0.02 * 5 + 0.10) / 6.0
    assert r["range_expansion_5_30m"] == pytest.approx(recent / trailing)


def test_range_expansion_flat_is_one() -> None:
    # A constant-range window -> recent mean == trailing mean -> ratio exactly 1.0 (no expansion).
    bars = [(101.0, 99.0, 100.0)] * 8
    out = run_group(REGISTRY.get_group("range_expansion"), BatchContext(frames={"minute_agg": _ohlc(bars)}))
    r = _row(out, BASE + timedelta(minutes=7))
    assert r["range_expansion_5_30m"] == pytest.approx(1.0)


def test_range_expansion_zero_close_excluded() -> None:
    # A zero-close bar contributes NULL (guarded close>0), excluded from both window means — not inf/nan.
    bars = [(101.0, 99.0, 100.0)] * 5 + [(105.0, 95.0, 0.0)]
    out = run_group(REGISTRY.get_group("range_expansion"), BatchContext(frames={"minute_agg": _ohlc(bars)}))
    r = _row(out, BASE + timedelta(minutes=5))
    # Both means now over the same five finite .02 bars -> ratio 1.0, finite (no div-by-zero).
    assert r["range_expansion_5_30m"] == pytest.approx(1.0)


def test_range_expansion_latest_parity_constant_window() -> None:
    bars = [(101.0, 99.0, 100.0)] * 8
    _assert_window_parity("range_expansion", {"minute_agg": _ohlc(bars)})


# --- print_hhi + size_entropy (Layer C, trades) ---


def _trades(rows: list[tuple[datetime, float, float]]) -> pl.DataFrame:
    """rows = list of (ts, price, size)."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(rows),
            "ts": [r[0] for r in rows],
            "price": [r[1] for r in rows],
            "size": [r[2] for r in rows],
        }
    )


def test_print_hhi_single_minute() -> None:
    # One minute, prints with notional [100*1, 100*1, 100*8] = [100, 100, 800] (price 100, sizes 1,1,8).
    # HHI = (100^2 + 100^2 + 800^2) / (1000^2) = (10000+10000+640000)/1_000_000 = 0.66.
    m0 = BASE
    rows = [(m0 + timedelta(seconds=1), 100.0, 1.0),
            (m0 + timedelta(seconds=2), 100.0, 1.0),
            (m0 + timedelta(seconds=3), 100.0, 8.0)]
    out = run_group(REGISTRY.get_group("print_hhi"), BatchContext(frames={"trades": _trades(rows)}))
    r = _row(out, m0)
    expected = (100.0**2 + 100.0**2 + 800.0**2) / (1000.0**2)
    assert r["print_hhi_30m"] == pytest.approx(expected)


def test_print_hhi_window_mean() -> None:
    # m0 HHI = 1.0 (single print). m1 HHI = 0.5 (two equal prints: (a^2+a^2)/(2a)^2 = 0.5).
    # print_hhi_30m at m1 = mean(1.0, 0.5) = 0.75.
    m0, m1 = BASE, BASE + timedelta(minutes=1)
    rows = [(m0 + timedelta(seconds=1), 100.0, 5.0),
            (m1 + timedelta(seconds=1), 100.0, 3.0),
            (m1 + timedelta(seconds=2), 100.0, 3.0)]
    out = run_group(REGISTRY.get_group("print_hhi"), BatchContext(frames={"trades": _trades(rows)}))
    r = _row(out, m1)
    assert r["print_hhi_30m"] == pytest.approx((1.0 + 0.5) / 2.0)


def test_print_hhi_window_parity() -> None:
    m0, m1, m2 = BASE, BASE + timedelta(minutes=1), BASE + timedelta(minutes=2)
    rows = [(m0 + timedelta(seconds=1), 100.0, 5.0),
            (m1 + timedelta(seconds=1), 100.0, 3.0),
            (m1 + timedelta(seconds=2), 100.0, 7.0),
            (m2 + timedelta(seconds=1), 100.0, 4.0),
            (m2 + timedelta(seconds=2), 100.0, 4.0)]
    _assert_window_parity("print_hhi", {"trades": _trades(rows)})


def test_print_hhi_empty_frame() -> None:
    empty = pl.DataFrame(
        schema={"symbol": pl.String, "ts": pl.Datetime("us", "UTC"), "price": pl.Float64, "size": pl.Float64}
    )
    out = run_group(REGISTRY.get_group("print_hhi"), BatchContext(frames={"trades": empty}))
    assert out.height == 0
    assert set(out.columns) == {"symbol", "minute", "print_hhi_30m", "print_hhi_60m"}


def test_size_entropy_uniform_max() -> None:
    # One print in each of the 6 size bins -> uniform distribution -> entropy = ln(6).
    # Sizes: 5 (bin0: 10^0..1), 50 (bin1), 500 (bin2), 5000 (bin3), 50000 (bin4), 500000 (bin5).
    m0 = BASE
    sizes = [5.0, 50.0, 500.0, 5000.0, 50000.0, 500000.0]
    rows = [(m0 + timedelta(seconds=i + 1), 100.0, sizes[i]) for i in range(6)]
    out = run_group(REGISTRY.get_group("size_entropy"), BatchContext(frames={"trades": _trades(rows)}))
    r = _row(out, m0)
    assert r["size_entropy_30m"] == pytest.approx(math.log(6.0))


def test_size_entropy_single_scale_zero() -> None:
    # All prints in ONE size bin -> degenerate distribution -> entropy exactly 0 (not nan).
    m0 = BASE
    rows = [(m0 + timedelta(seconds=i + 1), 100.0, 30.0) for i in range(5)]  # all bin1 (10-99)
    out = run_group(REGISTRY.get_group("size_entropy"), BatchContext(frames={"trades": _trades(rows)}))
    r = _row(out, m0)
    assert r["size_entropy_30m"] == pytest.approx(0.0)


def test_size_entropy_window_sum() -> None:
    # m0: 2 prints in bin1. m1: 2 prints in bin3. Over the window: counts {bin1:2, bin3:2} -> uniform
    # over 2 occupied bins -> p=0.5,0.5 -> entropy = ln(2).
    m0, m1 = BASE, BASE + timedelta(minutes=1)
    rows = [(m0 + timedelta(seconds=1), 100.0, 20.0),
            (m0 + timedelta(seconds=2), 100.0, 40.0),
            (m1 + timedelta(seconds=1), 100.0, 2000.0),
            (m1 + timedelta(seconds=2), 100.0, 4000.0)]
    out = run_group(REGISTRY.get_group("size_entropy"), BatchContext(frames={"trades": _trades(rows)}))
    r = _row(out, m1)
    assert r["size_entropy_30m"] == pytest.approx(math.log(2.0))


def test_size_entropy_degenerate_window_parity() -> None:
    # YELLOW requirement: dedicated degenerate-window live==backfill parity (single-scale zero-entropy
    # AND a constant-per-minute-count window — the corners where a naive entropy could diverge).
    m0, m1, m2 = BASE, BASE + timedelta(minutes=1), BASE + timedelta(minutes=2)
    single_scale = [(m + timedelta(seconds=s), 100.0, 30.0) for m in (m0, m1, m2) for s in (1, 2)]
    _assert_window_parity("size_entropy", {"trades": _trades(single_scale)})


def test_size_entropy_window_parity_mixed() -> None:
    m0, m1, m2 = BASE, BASE + timedelta(minutes=1), BASE + timedelta(minutes=2)
    rows = [(m0 + timedelta(seconds=1), 100.0, 5.0),
            (m0 + timedelta(seconds=2), 100.0, 5000.0),
            (m1 + timedelta(seconds=1), 100.0, 50.0),
            (m2 + timedelta(seconds=1), 100.0, 500.0),
            (m2 + timedelta(seconds=2), 100.0, 50000.0)]
    _assert_window_parity("size_entropy", {"trades": _trades(rows)})


def test_size_entropy_empty_frame() -> None:
    empty = pl.DataFrame(
        schema={"symbol": pl.String, "ts": pl.Datetime("us", "UTC"), "price": pl.Float64, "size": pl.Float64}
    )
    out = run_group(REGISTRY.get_group("size_entropy"), BatchContext(frames={"trades": empty}))
    assert out.height == 0
    assert set(out.columns) == {"symbol", "minute", "size_entropy_30m", "size_entropy_60m"}
