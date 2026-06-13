"""Unit tests for the candlestick / trend_quality / price_volume families (Layer A).

Hand-built bars with known geometry lock in the pattern detection and the rolling-OLS math, so a
regression in the vectorized expressions fails loudly. Parity for these groups is covered by the
shared T+1 harness (they self-select via ``runnable`` once ``open``/``volume`` are present); these
tests pin the per-cell values the harness compares.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
# A correct live buffer must exceed the largest feature window (price_volume 120m here) + lag, or the
# buffer's leading-edge minutes lose lag context and diverge from backfill. 150 > 120 + headroom.
LIVE_WINDOW = 150


def _ohlc(bars: list[tuple[float, float, float, float, float]]) -> pl.DataFrame:
    """bars = list of (open, high, low, close, volume) on a contiguous one-minute AAA grid."""
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(bars),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(bars))],
            "open": [b[0] for b in bars],
            "high": [b[1] for b in bars],
            "low": [b[2] for b in bars],
            "close": [b[3] for b in bars],
            "volume": [b[4] for b in bars],
        }
    )


def _row(out: pl.DataFrame, i: int) -> dict:
    return out.filter(pl.col("minute") == BASE + timedelta(minutes=i)).row(0, named=True)


# --- candlestick ---


def test_candlestick_single_bar_shapes() -> None:
    frame = _ohlc(
        [
            (100.0, 102.0, 98.0, 100.02, 1000.0),  # m0 doji: body 0.02 / range 4
            (100.0, 101.0, 100.0, 101.0, 1000.0),  # m1 marubozu bull: body fills range
            (100.0, 100.25, 99.0, 100.2, 1000.0),  # m2 hammer: long lower wick, small body at top
        ]
    )
    out = run_group(REGISTRY.get_group("candlestick"), BatchContext(frames={"minute_agg": frame}))
    m0, m1, m2 = _row(out, 0), _row(out, 1), _row(out, 2)
    assert m0["is_doji"] == 1.0 and m0["is_marubozu"] == 0.0
    assert m1["is_marubozu"] == 1.0 and m1["is_bullish"] == 1.0 and m1["is_doji"] == 0.0
    assert m1["body_ratio"] == pytest.approx(1.0)
    assert m2["is_hammer"] == 1.0 and m2["is_shooting_star"] == 0.0
    # two-candle patterns are warmup-null on the first bar (no prior)
    assert m0["pattern_engulfing_bullish"] is None


def test_candlestick_engulfing_and_harami() -> None:
    frame = _ohlc(
        [
            (101.0, 101.2, 99.8, 100.0, 1000.0),   # m0 bearish (open>close)
            (99.9, 101.3, 99.7, 101.1, 1000.0),    # m1 bullish body engulfs m0 body
            (100.0, 102.0, 99.0, 101.5, 1000.0),   # m2 large bullish
            (101.3, 101.4, 100.6, 100.8, 1000.0),  # m3 small bearish inside m2 body -> harami_bearish
        ]
    )
    out = run_group(REGISTRY.get_group("candlestick"), BatchContext(frames={"minute_agg": frame}))
    assert _row(out, 1)["pattern_engulfing_bullish"] == 1.0
    assert _row(out, 3)["pattern_harami_bearish"] == 1.0


def test_candlestick_zero_range_is_not_nan() -> None:
    frame = _ohlc([(100.0, 100.0, 100.0, 100.0, 1000.0)])  # flat bar, range 0
    out = run_group(REGISTRY.get_group("candlestick"), BatchContext(frames={"minute_agg": frame}))
    m0 = _row(out, 0)
    assert m0["body_ratio"] == 0.0 and m0["is_doji"] == 1.0  # mapped to 0, not NaN


# --- trend_quality ---


def test_trend_quality_perfect_line() -> None:
    closes = [100.0 + i * 0.5 for i in range(12)]  # exactly linear rising
    frame = _ohlc([(c, c + 0.1, c - 0.1, c, 1000.0) for c in closes])
    out = run_group(REGISTRY.get_group("trend_quality"), BatchContext(frames={"minute_agg": frame}))
    late = _row(out, 11)
    assert late["price_r2_5m"] == pytest.approx(1.0, abs=1e-6)  # straight line -> R^2 = 1
    # 5m window at minute 11 covers minutes 7..11 -> mean close 100 + 9*0.5 = 104.5; slope 0.5/min
    assert late["price_slope_5m"] == pytest.approx(0.5 / 104.5, rel=1e-4)
    assert late["trend_strength_5m"] == pytest.approx(late["price_slope_5m"] * late["price_r2_5m"], rel=1e-4)


def test_trend_quality_downtrend_is_negative() -> None:
    closes = [120.0 - i * 0.4 for i in range(12)]
    frame = _ohlc([(c, c + 0.1, c - 0.1, c, 1000.0) for c in closes])
    out = run_group(REGISTRY.get_group("trend_quality"), BatchContext(frames={"minute_agg": frame}))
    late = _row(out, 11)
    assert late["price_slope_10m"] < 0.0 and late["trend_strength_10m"] < 0.0
    assert late["price_r2_10m"] == pytest.approx(1.0, abs=1e-6)


# --- price_volume ---


def test_price_volume_all_up_bars() -> None:
    closes = [100.0 + i * 0.5 for i in range(10)]  # strictly rising -> every bar an up-bar
    frame = _ohlc([(c - 0.3, c, c - 0.4, c, 1000.0) for c in closes])  # close == high -> mfm = 1
    out = run_group(REGISTRY.get_group("price_volume"), BatchContext(frames={"minute_agg": frame}))
    late = _row(out, 9)  # 5m window (minutes 5..9) excludes the warmup-null first bar
    assert late["up_volume_ratio_5m"] == pytest.approx(1.0)
    assert late["down_volume_ratio_5m"] == pytest.approx(0.0)
    assert late["volume_delta_5m"] == pytest.approx(1.0)
    assert late["buying_pressure_5m"] == pytest.approx(1.0)  # closes at the high every bar
    assert late["vwap_deviation_5m"] > 0.0  # rising close sits above its trailing vwap


def test_price_volume_down_bar_pressure() -> None:
    closes = [100.0 - i * 0.5 for i in range(10)]  # strictly falling
    frame = _ohlc([(c + 0.3, c + 0.4, c, c, 1000.0) for c in closes])  # close == low -> mfm = -1
    out = run_group(REGISTRY.get_group("price_volume"), BatchContext(frames={"minute_agg": frame}))
    late = _row(out, 9)
    assert late["down_volume_ratio_5m"] == pytest.approx(1.0)
    assert late["volume_delta_5m"] == pytest.approx(-1.0)
    assert late["buying_pressure_5m"] == pytest.approx(-1.0)


# --- live-buffer vs backfill parity (the rebuild's whole point) ---


def _wavy_ohlc(n: int) -> pl.DataFrame:
    """A non-trivial OHLCV series: a trend with curvature and noise so the rolling features actually
    vary (a straight line would make R^2 a constant and hide a centering bug)."""
    import math

    bars = []
    for i in range(n):
        close = 100.0 + 5.0 * math.sin(i / 11.0) + i * 0.03 + (0.2 if i % 5 == 0 else -0.1)
        bars.append((close - 0.04, close + 0.09, close - 0.08, close, 800.0 + (i % 13) * 40.0))
    return _ohlc(bars)


def _replay_live(group, full: pl.DataFrame, window: int = LIVE_WINDOW) -> pl.DataFrame:
    """Reproduce the live path: at each minute, compute the group over ONLY the trailing ``window``
    minutes of bars (the streaming buffer) and keep that minute's row, exactly as
    capture.process_bars does. The accumulation of these per-minute live rows is what must match the
    one-shot backfill over the full series."""
    minutes = sorted(full["minute"].unique())
    rows = []
    for i, minute in enumerate(minutes):
        buf_minutes = minutes[max(0, i - window + 1): i + 1]
        buf = full.filter(pl.col("minute").is_in(buf_minutes))
        out = run_group(group, BatchContext(frames={"minute_agg": buf}), validate=False)
        rows.append(out.filter(pl.col("minute") == minute))
    return pl.concat(rows)


@pytest.mark.parametrize("group_name", ["candlestick", "trend_quality", "price_volume"])
def test_live_buffer_matches_backfill(group_name: str) -> None:
    group = REGISTRY.get_group(group_name)
    full = _wavy_ohlc(220)  # > LIVE_WINDOW so the trailing buffer genuinely evicts early minutes
    backfill = run_group(group, BatchContext(frames={"minute_agg": full}), validate=False)
    live = _replay_live(group, full)

    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    joined = live.join(backfill, on=["symbol", "minute"], suffix="_bk")
    for feature, tol in tolerances.items():
        pairs = joined.select(["minute", feature, f"{feature}_bk"]).drop_nulls()
        assert pairs.height > 0, f"{feature}: no settled cells to compare"
        within = pairs.select(
            ((pl.col(feature) - pl.col(f"{feature}_bk")).abs() <= 1e-12 + tol * pl.col(f"{feature}_bk").abs()).all()
        ).item()
        assert within, f"{feature}: live trailing-buffer value diverged from backfill beyond tol={tol}"


def test_undersized_buffer_diverges() -> None:
    """The buffer-size invariant BITES: a buffer equal to the feature window (one short of the lag it
    needs) makes the 60m window's leading-edge minute lose its return, so live != backfill. This is
    the exact failure class the rebuild exists to catch — proven here so the 300-min default is not
    cargo-culted."""
    group = REGISTRY.get_group("price_volume")
    full = _wavy_ohlc(160)
    backfill = run_group(group, BatchContext(frames={"minute_agg": full}), validate=False)
    live_bad = _replay_live(group, full, window=60)  # < the 60m+lag the feature needs
    joined = live_bad.join(backfill, on=["symbol", "minute"], suffix="_bk")
    pairs = joined.select(["up_volume_ratio_60m", "up_volume_ratio_60m_bk"]).drop_nulls()
    diverged = pairs.select(
        ((pl.col("up_volume_ratio_60m") - pl.col("up_volume_ratio_60m_bk")).abs() > 1e-6).any()
    ).item()
    assert diverged  # undersized buffer MUST be detectable as a parity break
