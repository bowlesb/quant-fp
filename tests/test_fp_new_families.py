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
