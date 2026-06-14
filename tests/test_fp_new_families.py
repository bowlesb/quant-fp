"""Unit tests for the candlestick / trend_quality / price_volume families (Layer A).

Hand-built bars with known geometry lock in the pattern detection and the rolling-OLS math, so a
regression in the vectorized expressions fails loudly. Parity for these groups is covered by the
shared T+1 harness (they self-select via ``runnable`` once ``open``/``volume`` are present); these
tests pin the per-cell values the harness compares.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
# A correct live buffer must exceed the largest feature window (180m across these groups) + lag, or
# the buffer's leading-edge minutes lose lag context and diverge from backfill. 210 > 180 + headroom.
LIVE_WINDOW = 210


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


def test_efficiency_clean_vs_chop() -> None:
    rising = _ohlc([(c, c + 0.1, c - 0.1, c, 1000.0) for c in [100.0 + i * 0.5 for i in range(15)]])
    out = run_group(REGISTRY.get_group("efficiency"), BatchContext(frames={"minute_agg": rising}))
    late = _row(out, 14)
    assert late["efficiency_ratio_10m"] == pytest.approx(1.0)  # monotonic: net travel == total travel
    assert late["directional_efficiency_10m"] == pytest.approx(1.0)
    # a saw-tooth that returns to its start travels far but nets ~0 -> low efficiency
    saw = [100.0 + (1.0 if i % 2 else 0.0) for i in range(15)]
    chop = _ohlc([(c, c + 0.1, c - 0.1, c, 1000.0) for c in saw])
    out2 = run_group(REGISTRY.get_group("efficiency"), BatchContext(frames={"minute_agg": chop}))
    assert out2.filter(pl.col("minute") == BASE + timedelta(minutes=14)).row(0, named=True)["efficiency_ratio_10m"] < 0.3


def test_distribution_semivariance_and_skew() -> None:
    # returns with occasional large DOWN moves -> negative skew, downside_vol > upside_vol
    import math

    closes = [100.0]
    for i in range(1, 60):
        step = -2.0 if i % 11 == 0 else 0.15 * math.cos(i / 3.0)
        closes.append(closes[-1] + step)
    frame = _ohlc([(c, c + 0.05, c - 0.05, c, 1000.0) for c in closes])
    out = run_group(REGISTRY.get_group("distribution"), BatchContext(frames={"minute_agg": frame}))
    late = _row(out, 59)
    assert late["ret_skew_30m"] < 0.0  # down-shocks make the distribution left-skewed
    assert late["downside_vol_30m"] > late["upside_vol_30m"]


def test_market_beta_exact_relationship() -> None:
    import math

    rets = [0.002 * math.sin(i / 5.0) for i in range(80)]
    spy, aaa = [100.0], [50.0]
    for r in rets[1:]:
        spy.append(spy[-1] * (1.0 + r))
        aaa.append(aaa[-1] * (1.0 + 2.0 * r))  # AAA moves exactly 2x SPY each minute
    rows = []
    for symbol, closes in (("SPY", spy), ("AAA", aaa)):
        for i, c in enumerate(closes):
            rows.append({"symbol": symbol, "minute": BASE + timedelta(minutes=i), "open": c, "high": c,
                         "low": c, "close": c, "volume": 1000.0})
    out = run_group(REGISTRY.get_group("market_beta"), BatchContext(frames={"minute_agg": pl.DataFrame(rows)}))
    aaa_late = out.filter((pl.col("symbol") == "AAA") & (pl.col("minute") == BASE + timedelta(minutes=79))).row(0, named=True)
    assert aaa_late["market_beta_30m"] == pytest.approx(2.0, rel=1e-4)
    assert aaa_late["market_corr_30m"] == pytest.approx(1.0, rel=1e-4)
    assert aaa_late["idio_vol_30m"] == pytest.approx(0.0, abs=1e-6)  # fully explained by the market


@pytest.mark.parametrize("group_name", ["candlestick", "trend_quality", "price_volume", "efficiency", "distribution", "ohlc_vol", "return_dynamics", "calendar_events", "round_levels"])
def test_live_buffer_matches_backfill(group_name: str) -> None:
    group = REGISTRY.get_group(group_name)
    full = _wavy_ohlc(260)  # > LIVE_WINDOW so the trailing buffer genuinely evicts early minutes
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


def _multi_ohlc(symbols: tuple[str, ...], n: int) -> pl.DataFrame:
    """Distinct wavy OHLCV series per symbol (each phase-shifted) so index and ticker returns differ."""
    import math

    rows = []
    for s_idx, symbol in enumerate(symbols):
        for i in range(n):
            close = 100.0 + 4.0 * math.sin((i + s_idx * 7) / 9.0) + i * (0.02 + 0.01 * s_idx)
            rows.append(
                {"symbol": symbol, "minute": BASE + timedelta(minutes=i), "open": close - 0.04,
                 "high": close + 0.09, "low": close - 0.08, "close": close, "volume": 900.0 + (i % 11) * 30.0}
            )
    return pl.DataFrame(rows)


def test_market_context_broadcast_and_relative() -> None:
    frame = _multi_ohlc(("SPY", "QQQ", "AAA"), 30)
    out = run_group(REGISTRY.get_group("market_context"), BatchContext(frames={"minute_agg": frame}))
    minute = BASE + timedelta(minutes=20)
    at = out.filter(pl.col("minute") == minute)
    spy = at.filter(pl.col("symbol") == "SPY").row(0, named=True)
    aaa = at.filter(pl.col("symbol") == "AAA").row(0, named=True)
    # the index return is broadcast: identical for every ticker at this minute
    assert aaa["market_return_5m"] == pytest.approx(spy["market_return_5m"])
    # SPY's own return relative to SPY is zero, so it never "outperforms" itself
    assert spy["relative_return_5m"] == pytest.approx(0.0, abs=1e-12)
    assert spy["outperforming_5m"] == 0.0
    # AAA's relative return is defined and its outperforming flag agrees with the sign
    assert aaa["relative_return_5m"] is not None
    assert aaa["outperforming_5m"] == (1.0 if aaa["relative_return_5m"] > 0 else 0.0)


@pytest.mark.parametrize("group_name", ["market_context", "market_beta", "cross_sectional_rank"])
def test_market_context_live_buffer_matches_backfill(group_name: str) -> None:
    group = REGISTRY.get_group(group_name)
    full = _multi_ohlc(("SPY", "QQQ", "AAA", "BBB"), 260)
    backfill = run_group(group, BatchContext(frames={"minute_agg": full}), validate=False)
    live = _replay_live(group, full)
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    joined = live.join(backfill, on=["symbol", "minute"], suffix="_bk")
    for feature, tol in tolerances.items():
        pairs = joined.select([feature, f"{feature}_bk"]).drop_nulls()
        assert pairs.height > 0, f"{feature}: no settled cells"
        within = pairs.select(
            ((pl.col(feature) - pl.col(f"{feature}_bk")).abs() <= 1e-12 + tol * pl.col(f"{feature}_bk").abs()).all()
        ).item()
        assert within, f"{feature}: live trailing-buffer diverged from backfill beyond tol={tol}"


# --- ohlc_vol / return_dynamics / calendar_events / cross_sectional_rank ---


def test_ohlc_vol_known_values() -> None:
    import math

    # identical bars O=C=100, H=101, L=99 -> gk_var = rs_var = 0.5*ln(101/99)^2 (the ln(C/O) term is 0)
    frame = _ohlc([(100.0, 101.0, 99.0, 100.0, 1000.0) for _ in range(12)])
    out = run_group(REGISTRY.get_group("ohlc_vol"), BatchContext(frames={"minute_agg": frame}))
    late = _row(out, 11)
    expected_gk = math.sqrt(0.5 * math.log(101.0 / 99.0) ** 2)  # ln(C/O) term is 0 since C==O
    # O==C so ln(H/C)=ln(H/O) and ln(L/C)=ln(L/O): rs_var = ln(H/C)^2 + ln(L/C)^2
    expected_rs = math.sqrt(math.log(101.0 / 100.0) ** 2 + math.log(99.0 / 100.0) ** 2)
    assert late["garman_klass_vol_5m"] == pytest.approx(expected_gk, rel=1e-9)
    assert late["rogers_satchell_vol_5m"] == pytest.approx(expected_rs, rel=1e-9)


def test_return_dynamics_mean_reversion() -> None:
    closes = [100.0 + (1.0 if i % 2 else 0.0) for i in range(40)]  # alternating -> mean-reverting
    frame = _ohlc([(c, c + 0.1, c - 0.1, c, 1000.0) for c in closes])
    out = run_group(REGISTRY.get_group("return_dynamics"), BatchContext(frames={"minute_agg": frame}))
    assert _row(out, 39)["autocorr_1_30m"] < -0.5  # sign flips every step -> strong negative lag-1


def test_calendar_events_triple_witching() -> None:
    # 2026-06-19 is the 3rd Friday of June (a quarter-end month) -> OPEX + triple witching
    minutes = [
        datetime(2026, 6, 19, 14, 0, tzinfo=timezone.utc),  # 10:00 ET, same date
        datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc),  # 2nd Friday -> not OPEX
    ]
    frame = pl.DataFrame({"symbol": ["AAA", "AAA"], "minute": minutes})
    out = run_group(REGISTRY.get_group("calendar_events"), BatchContext(frames={"minute_agg": frame}))
    opex = out.filter(pl.col("minute") == minutes[0]).row(0, named=True)
    plain = out.filter(pl.col("minute") == minutes[1]).row(0, named=True)
    assert opex["is_opex_day"] == 1.0 and opex["is_triple_witching"] == 1.0 and opex["is_quarter_end_month"] == 1.0
    assert opex["week_of_month"] == 3.0
    assert plain["is_opex_day"] == 0.0 and plain["is_triple_witching"] == 0.0


def test_cross_sectional_rank_ordering() -> None:
    minute = BASE
    rows = [
        {"symbol": "AAA", "minute": minute, "close": 100.0, "volume": 100.0},
        {"symbol": "BBB", "minute": minute, "close": 100.0, "volume": 200.0},
        {"symbol": "CCC", "minute": minute, "close": 100.0, "volume": 300.0},
    ]
    out = run_group(REGISTRY.get_group("cross_sectional_rank"), BatchContext(frames={"minute_agg": pl.DataFrame(rows)}))
    ranks = {r["symbol"]: r["volume_rank_1m"] for r in out.iter_rows(named=True)}
    assert ranks["AAA"] == pytest.approx(0.0) and ranks["BBB"] == pytest.approx(0.5) and ranks["CCC"] == pytest.approx(1.0)


# --- liquidity (Kyle lambda / Amihud / Roll) + round_levels ---


def _signed_frame(n: int) -> pl.DataFrame:
    """A frame with signed_volume where each price change is exactly 0.001 * signed_volume."""
    import math

    sv = [200.0 * math.sin(i / 4.0) + 50.0 * ((i % 3) - 1) for i in range(n)]
    close = [100.0]
    for i in range(1, n):
        close.append(close[-1] + 0.001 * sv[i])
    return pl.DataFrame(
        {"symbol": ["AAA"] * n, "minute": [BASE + timedelta(minutes=i) for i in range(n)],
         "close": close, "volume": [1000.0] * n, "signed_volume": sv}
    )


def test_liquidity_kyle_lambda_recovered() -> None:
    frame = _signed_frame(40)
    out = run_group(REGISTRY.get_group("liquidity"), BatchContext(frames={"minute_agg": frame}))
    late = out.filter(pl.col("minute") == BASE + timedelta(minutes=39)).row(0, named=True)
    assert late["kyle_lambda_30m"] == pytest.approx(0.001, rel=1e-4)  # dp = 0.001 * signed_volume
    assert late["amihud_illiq_30m"] > 0.0
    assert late["roll_spread_30m"] >= 0.0


def test_liquidity_live_buffer_matches_backfill() -> None:
    group = REGISTRY.get_group("liquidity")
    import math

    n = 260
    sv = [300.0 * math.sin(i / 7.0) + 40.0 * ((i % 5) - 2) for i in range(n)]
    close = [100.0 + 5.0 * math.sin(i / 11.0) + i * 0.02 for i in range(n)]
    full = pl.DataFrame(
        {"symbol": ["AAA"] * n, "minute": [BASE + timedelta(minutes=i) for i in range(n)],
         "close": close, "volume": [800.0 + (i % 13) * 40.0 for i in range(n)], "signed_volume": sv}
    )
    backfill = run_group(group, BatchContext(frames={"minute_agg": full}), validate=False)
    live = _replay_live(group, full)
    joined = live.join(backfill, on=["symbol", "minute"], suffix="_bk")
    for spec in group.declare():
        pairs = joined.select([spec.name, f"{spec.name}_bk"]).drop_nulls()
        within = pairs.select(
            ((pl.col(spec.name) - pl.col(f"{spec.name}_bk")).abs() <= 1e-12 + spec.tolerance * pl.col(f"{spec.name}_bk").abs()).all()
        ).item()
        assert within, f"{spec.name}: live buffer diverged from backfill"


def test_round_levels() -> None:
    frame = _ohlc([(100.01, 100.01, 100.01, 100.01, 1.0), (100.50, 100.50, 100.50, 100.50, 1.0)])
    out = run_group(REGISTRY.get_group("round_levels"), BatchContext(frames={"minute_agg": frame}))
    near = _row(out, 0)
    half = _row(out, 1)
    assert near["dist_to_round_dollar"] == pytest.approx(0.01) and near["is_at_round_dollar"] == 1.0
    assert half["dist_to_round_dollar"] == pytest.approx(0.5) and half["dist_to_half_dollar"] == pytest.approx(0.0)
    assert half["is_at_round_dollar"] == 0.0


# --- prior_day: gap + floor-trader pivots ---


def test_prior_day_pivots_and_gap() -> None:
    daily = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA"],
            "date": [date(2026, 6, 11), date(2026, 6, 12)],
            "open": [99.0, 103.0],
            "high": [105.0, 106.0],
            "low": [95.0, 102.0],
            "close": [102.0, 104.0],
        }
    )
    minute = pl.DataFrame({"symbol": ["AAA"], "minute": [datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)], "close": [104.0]})
    out = run_group(REGISTRY.get_group("prior_day"), BatchContext(frames={"daily": daily, "minute_agg": minute}))
    row = out.row(0, named=True)
    pivot = (105.0 + 95.0 + 102.0) / 3.0  # from the prior day (2026-06-11) OHLC
    assert row["gap_open"] == pytest.approx(103.0 / 102.0 - 1.0)  # today's open vs prior close
    assert row["dist_from_prior_high"] == pytest.approx(104.0 / 105.0 - 1.0)
    assert row["above_pivot"] == 1.0  # 104 > P
    assert row["dist_from_pivot_p"] == pytest.approx(104.0 / pivot - 1.0)
    assert row["dist_from_pivot_r2"] == pytest.approx(104.0 / (pivot + 10.0) - 1.0)  # R2 = P + (H-L)


def test_multi_day_vwap() -> None:
    # 7 daily bars; at the last date (D6=2026-06-11) the 5-day VWAP uses the prior 5 completed days
    # (vwap=100 each -> vwap_5d=100), and the prior close (close[D5]) is 105 -> dist=0.05, above=1.
    dates = [date(2026, 6, 5) + timedelta(days=i) for i in range(7)]
    closes = [100.0, 100.0, 100.0, 100.0, 100.0, 105.0, 999.0]  # close[D5]=105 is the prior close at D6
    daily = pl.DataFrame(
        {"symbol": ["AAA"] * 7, "date": dates, "close": closes, "volume": [1000.0] * 7, "vwap": [100.0] * 7}
    )
    minute = pl.DataFrame({"symbol": ["AAA"], "minute": [datetime(2026, 6, 11, 14, 0, tzinfo=timezone.utc)]})
    out = run_group(REGISTRY.get_group("multi_day_vwap"), BatchContext(frames={"daily": daily, "minute_agg": minute}))
    row = out.row(0, named=True)
    assert row["dist_from_vwap_5d"] == pytest.approx(0.05)
    assert row["above_vwap_5d"] == 1.0


# --- reference groups: sector one-hot + asset flags ---


def _ref_minutes(symbols: tuple[str, ...]) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": [s for s in symbols for _ in range(2)],
         "minute": [BASE + timedelta(minutes=i) for _ in symbols for i in range(2)]}
    )


def test_sector_one_hot() -> None:
    reference = pl.DataFrame(
        {"symbol": ["AAA", "BBB", "CCC"], "sector": ["Technology", None, "Financial Services"]}
    )
    minutes = _ref_minutes(("AAA", "BBB", "CCC"))
    out = run_group(REGISTRY.get_group("sector"), BatchContext(frames={"minute_agg": minutes, "reference": reference}))
    aaa = out.filter(pl.col("symbol") == "AAA").row(0, named=True)
    bbb = out.filter(pl.col("symbol") == "BBB").row(0, named=True)
    ccc = out.filter(pl.col("symbol") == "CCC").row(0, named=True)
    assert aaa["sector_is_technology"] == 1.0 and aaa["sector_is_unknown"] == 0.0
    assert bbb["sector_is_unknown"] == 1.0  # no mapped sector
    assert ccc["sector_is_financial_services"] == 1.0
    # one-hot: exactly one bucket set per row
    sector_cols = [c for c in out.columns if c.startswith("sector_is_")]
    row_sums = out.select(pl.sum_horizontal([pl.col(c) for c in sector_cols]).alias("s"))["s"].to_list()
    assert all(abs(value - 1.0) < 1e-12 for value in row_sums)


def test_asset_flags_mapping() -> None:
    reference = pl.DataFrame(
        {"symbol": ["AAA"], "shortable": [True], "easy_to_borrow": [False],
         "marginable": [True], "fractionable": [False]}
    )
    minutes = _ref_minutes(("AAA",))
    out = run_group(REGISTRY.get_group("asset_flags"), BatchContext(frames={"minute_agg": minutes, "reference": reference}))
    row = out.row(0, named=True)
    assert row["is_shortable"] == 1.0 and row["is_easy_to_borrow"] == 0.0
    assert row["is_marginable"] == 1.0 and row["is_fractionable"] == 0.0


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


# --- profiler (first-class per-group timing) ---


def test_profiler_covers_all_runnable_groups() -> None:
    from quantlib.features.profile import build_frames, profile

    frames = build_frames(n_tickers=40, window_min=30, daily_days=20)
    table = profile(frames, reps=1)
    assert table.height >= 20  # every runnable group timed
    assert set(table.columns) == {"group", "type", "n_features", "ms", "us_per_feature"}
    assert (table["ms"] >= 0.0).all() and (table["n_features"] > 0).all()
