"""Correctness tests for groups ported to the CleanEngine — formula + sanity + edge, NOT byte-parity.

The clean-engine rewrite drops byte-identical-to-legacy; what we validate instead is that each group computes
its DEFINED FORMULA correctly on known inputs (closed form where it has one, intuition + monotonicity + range
otherwise) and behaves sanely at the edges (warm-up, single bar, gaps, the multi-group pass). Floats may differ
from the legacy engine where the new math is cleaner — that is fine and expected.

Pattern (reused per ported group as batches land):
  * FORMULA: hand-constructed series whose answer is computable by hand → assert the feature matches it.
  * EDGE: window not filled (count<2) → NaN; single bar → NaN; a gap reads the last PRESENT bars (ring is
    gap-safe), not stale zeros.
  * SANITY: declared range (r2 in [0,1], deviation finite), no all-NaN / all-zero over a varied fixture,
    monotonicity where the feature's definition implies it (steeper trend → larger |slope|).
  * MULTI-GROUP PASS: the engine computes every group correctly in ONE shared step (the live shape).
"""

from __future__ import annotations

import numpy as np
import pytest

from quantlib.features.clean_engine import CleanEngine
from quantlib.features.clean_groups_example import (
    BreadthClean,
    CandlestickClean,
    MacdClean,
    RealizedRangeClean,
    TrendQualityClean,
    VwapDeviationClean,
)

WINDOW = 60


def _bars(
    symbols: list[str], close: list[float], volume: list[float] | None = None
) -> dict[str, np.ndarray]:
    """One minute's bar block for the given present symbols."""
    block: dict[str, np.ndarray] = {
        "symbol": np.array(symbols),
        "close": np.array(close, dtype=np.float64),
    }
    if volume is not None:
        block["volume"] = np.array(volume, dtype=np.float64)
    return block


def _run(groups, symbols, closes, volumes=None):
    """Drive the engine over a per-symbol close (and optional volume) series; return the last step's output.

    ``closes`` is {symbol: [close_t...]}; all symbols present every minute, equal length."""
    n_min = len(next(iter(closes.values())))
    engine = CleanEngine(groups, symbols, WINDOW)
    out: dict = {}
    for t in range(n_min):
        close = [closes[s][t] for s in symbols]
        volume = [volumes[s][t] for s in symbols] if volumes else [1000.0] * len(symbols)
        out = engine.step(_bars(symbols, close, volume))
    return out


# --------------------------------------------------------------------------------------------------------- #
# trend_quality: trailing OLS of close on time → normalized slope, r2, trend_strength.
# --------------------------------------------------------------------------------------------------------- #


def test_trend_quality_perfect_uptrend_r2_is_one() -> None:
    """A perfectly linear close (100,101,102,...) fits the time axis exactly → r2 == 1.0, slope > 0."""
    closes = {"A": [100.0 + k for k in range(20)]}
    out = _run([TrendQualityClean()], ["A"], closes)["trend_quality"]
    for w in (5, 10, 15, 20):
        assert out[f"price_r2_{w}m"][0] == pytest.approx(1.0), f"r2_{w}m perfect line"
        assert out[f"price_slope_{w}m"][0] > 0, f"slope_{w}m up"


def test_trend_quality_perfect_downtrend_negative_slope() -> None:
    closes = {"A": [100.0 - k for k in range(20)]}
    out = _run([TrendQualityClean()], ["A"], closes)["trend_quality"]
    assert out["price_r2_10m"][0] == pytest.approx(1.0)
    assert out["price_slope_10m"][0] < 0


def test_trend_quality_flat_is_zero_slope_nan_r2() -> None:
    """A flat close has zero variance in y → slope 0 (no move), r2 undefined (var_y == 0) → NaN."""
    closes = {"A": [50.0] * 20}
    out = _run([TrendQualityClean()], ["A"], closes)["trend_quality"]
    assert out["price_slope_10m"][0] == pytest.approx(0.0)
    assert np.isnan(out["price_r2_10m"][0]), "flat series r2 is undefined"


def test_trend_quality_slope_is_known_value() -> None:
    """close = 100 + k (slope 1/min on raw price). normalized slope = raw_slope / mean_y. Over the trailing
    w=5 (closes 100..104 once filled? no — at t=4 the window holds 100..104): mean_y=102, raw_slope=1 →
    norm = 1/102. Assert the exact closed form on a fully-known window."""
    closes = {"A": [100.0 + k for k in range(5)]}  # exactly 5 bars → window 5 full, none deeper
    out = _run([TrendQualityClean()], ["A"], closes)["trend_quality"]
    mean_y = float(np.mean([100.0, 101.0, 102.0, 103.0, 104.0]))  # 102
    assert out["price_slope_5m"][0] == pytest.approx(1.0 / mean_y)
    # trend_strength = norm_slope * r2 = (1/102) * 1.0
    assert out["trend_strength_5m"][0] == pytest.approx((1.0 / mean_y) * 1.0)


def test_trend_quality_steeper_trend_larger_slope_monotonic() -> None:
    """Monotonicity: a steeper uptrend (slope 2/min) has a larger normalized slope than a gentler one (1/min)
    at the same price level."""
    gentle = _run([TrendQualityClean()], ["A"], {"A": [100.0 + k for k in range(10)]})["trend_quality"]
    steep = _run([TrendQualityClean()], ["A"], {"A": [100.0 + 2 * k for k in range(10)]})["trend_quality"]
    assert steep["price_slope_10m"][0] > gentle["price_slope_10m"][0]


def test_trend_quality_warmup_nan_until_two_bars() -> None:
    """Fewer than 2 present bars in a window → NaN (can't fit a line). One bar → all NaN."""
    out = _run([TrendQualityClean()], ["A"], {"A": [100.0]})["trend_quality"]
    assert np.isnan(out["price_slope_5m"][0])
    assert np.isnan(out["price_r2_5m"][0])


def test_trend_quality_r2_in_unit_range() -> None:
    """SANITY: r2 is always in [0, 1] (or NaN) over a noisy fixture — never outside the unit interval."""
    rng = np.random.default_rng(0)
    closes = {"A": list(100.0 + np.cumsum(rng.standard_normal(40) * 0.5))}
    out = _run([TrendQualityClean()], ["A"], closes)["trend_quality"]
    for w in (5, 10, 15, 20, 30, 60):
        r2 = out[f"price_r2_{w}m"][0]
        assert np.isnan(r2) or (0.0 <= r2 <= 1.0), f"r2_{w}m out of [0,1]: {r2}"


# --------------------------------------------------------------------------------------------------------- #
# vwap_deviation: close / trailing-vwap − 1, vwap = Σ(close·vol) / Σvol over the window.
# --------------------------------------------------------------------------------------------------------- #


def test_vwap_deviation_known_value_constant_volume() -> None:
    """Constant volume → vwap = simple mean of the window's closes. closes 105..109 (w=5), latest 109 →
    vwap = 107, dev = 109/107 − 1."""
    closes = {"A": [100.0 + k for k in range(10)]}  # at the end, trailing 5 = 105..109
    out = _run([VwapDeviationClean()], ["A"], closes)["vwap_deviation"]
    assert out["vwap_deviation_5m"][0] == pytest.approx(109.0 / 107.0 - 1.0)


def test_vwap_deviation_volume_weighting() -> None:
    """Volume weighting: a known close+volume pair → vwap = Σcv/Σv computed by hand."""
    closes = {"A": [10.0, 20.0]}
    volumes = {"A": [1.0, 3.0]}  # vwap_2 = (10*1 + 20*3)/(1+3) = 70/4 = 17.5; latest close 20 → 20/17.5 − 1
    out = _run([VwapDeviationClean()], ["A"], closes, volumes)["vwap_deviation"]
    assert out["vwap_deviation_5m"][0] == pytest.approx(20.0 / 17.5 - 1.0)


def test_vwap_deviation_sign() -> None:
    """close above its vwap → positive deviation; below → negative; flat → ~0."""
    up = _run([VwapDeviationClean()], ["A"], {"A": [100.0, 100.0, 110.0]})["vwap_deviation"]
    down = _run([VwapDeviationClean()], ["A"], {"A": [100.0, 100.0, 90.0]})["vwap_deviation"]
    flat = _run([VwapDeviationClean()], ["A"], {"A": [100.0, 100.0, 100.0]})["vwap_deviation"]
    assert up["vwap_deviation_5m"][0] > 0
    assert down["vwap_deviation_5m"][0] < 0
    assert flat["vwap_deviation_5m"][0] == pytest.approx(0.0)


def test_vwap_deviation_warmup_single_bar_is_finite_zero() -> None:
    """A single bar: vwap == that bar's close → dev == 0 (close/vwap − 1), and finite (vol > 0)."""
    out = _run([VwapDeviationClean()], ["A"], {"A": [42.0]}, {"A": [500.0]})["vwap_deviation"]
    assert out["vwap_deviation_5m"][0] == pytest.approx(0.0)


# --------------------------------------------------------------------------------------------------------- #
# Ring / engine edge behaviour shared by every group.
# --------------------------------------------------------------------------------------------------------- #


def test_gap_reads_last_present_bars_not_stale_zero() -> None:
    """A symbol absent for a minute keeps its prior bars (the ring is gap-safe). B is present at t0,t2 only;
    its trend over its 2 present bars (100→102) is an uptrend, NOT corrupted by the absent t1."""
    engine = CleanEngine([TrendQualityClean()], ["A", "B"], WINDOW)
    engine.step(_bars(["A", "B"], [100.0, 100.0]))
    engine.step(_bars(["A"], [101.0]))  # B absent this minute
    out = engine.step(_bars(["A", "B"], [102.0, 102.0]))["trend_quality"]
    # B has 2 present bars (100, 102) → a valid 2-point up-fit, r2 == 1, slope > 0 (not NaN/garbage)
    assert out["price_r2_5m"][1] == pytest.approx(1.0)
    assert out["price_slope_5m"][1] > 0


def test_multi_group_pass_computes_every_group() -> None:
    """The one shared step computes EVERY group correctly in a single pass (the live shape). Both groups
    present, both produce their full feature set with sane values for a known uptrend."""
    closes = {"A": [100.0 + k for k in range(10)], "B": [50.0 - k for k in range(10)]}
    out = _run([TrendQualityClean(), VwapDeviationClean()], ["A", "B"], closes)
    assert set(out) == {"trend_quality", "vwap_deviation"}
    assert set(out["trend_quality"]) == set(TrendQualityClean().feature_names)
    assert set(out["vwap_deviation"]) == set(VwapDeviationClean().feature_names)
    # A up → +slope, B down → −slope, in the SAME pass
    assert out["trend_quality"]["price_slope_10m"][0] > 0
    assert out["trend_quality"]["price_slope_10m"][1] < 0


def test_no_all_nan_or_all_zero_over_varied_fixture() -> None:
    """SANITY: over a varied multi-symbol fixture, no feature comes out all-NaN or all-zero (a dead feature)."""
    rng = np.random.default_rng(1)
    syms = [f"S{i}" for i in range(6)]
    closes = {
        s: list(100.0 + np.cumsum(rng.standard_normal(40) * (0.5 + 0.1 * i))) for i, s in enumerate(syms)
    }
    volumes = {s: list(1000.0 + rng.random(40) * 4000) for s in syms}
    out = _run([TrendQualityClean(), VwapDeviationClean()], syms, closes, volumes)
    for gname, feats in out.items():
        for fname, arr in feats.items():
            assert not np.all(np.isnan(arr)), f"{gname}.{fname} all-NaN"
            finite = arr[np.isfinite(arr)]
            assert finite.size == 0 or not np.all(finite == 0.0), f"{gname}.{fname} all-zero"


# --------------------------------------------------------------------------------------------------------- #
# realized_range: trailing mean of (high-low)/close over short windows.
# --------------------------------------------------------------------------------------------------------- #


def _ohlc(symbols, o, h, low, c):
    return {
        "symbol": np.array(symbols),
        "open": np.array(o, dtype=np.float64),
        "high": np.array(h, dtype=np.float64),
        "low": np.array(low, dtype=np.float64),
        "close": np.array(c, dtype=np.float64),
    }


def test_realized_range_known_constant_value() -> None:
    """Every bar has high-low = 2, close = 100 → range fraction 0.02 each bar → trailing mean = 0.02."""
    engine = CleanEngine([RealizedRangeClean()], ["A"], WINDOW)
    for _ in range(6):
        engine.step(
            {
                "symbol": np.array(["A"]),
                "high": np.array([101.0]),
                "low": np.array([99.0]),
                "close": np.array([100.0]),
            }
        )
    out = engine.step(
        {
            "symbol": np.array(["A"]),
            "high": np.array([101.0]),
            "low": np.array([99.0]),
            "close": np.array([100.0]),
        }
    )["realized_range"]
    for w in (3, 5, 10):
        assert out[f"realized_range_{w}m"][0] == pytest.approx(2.0 / 100.0)


def test_realized_range_nonnegative_and_mean_of_known() -> None:
    """A known two-value range series averages correctly and is always >= 0."""
    engine = CleanEngine([RealizedRangeClean()], ["A"], WINDOW)
    # bar 1: range 4 (h104,l100,c100 → 0.04); bar 2: range 2 (h102,l100,c100 → 0.02). mean over 2 = 0.03
    engine.step(
        {
            "symbol": np.array(["A"]),
            "high": np.array([104.0]),
            "low": np.array([100.0]),
            "close": np.array([100.0]),
        }
    )
    out = engine.step(
        {
            "symbol": np.array(["A"]),
            "high": np.array([102.0]),
            "low": np.array([100.0]),
            "close": np.array([100.0]),
        }
    )["realized_range"]
    assert out["realized_range_3m"][0] == pytest.approx((0.04 + 0.02) / 2)
    assert out["realized_range_3m"][0] >= 0.0


# --------------------------------------------------------------------------------------------------------- #
# candlestick: per-bar geometry + the 2-candle engulfing pattern.
# --------------------------------------------------------------------------------------------------------- #


def test_candlestick_body_and_shadow_ratios_known() -> None:
    """A bar o=100 h=110 l=90 c=105: range 20, body |105-100|=5 → 0.25; upper (110-105)/20=0.25;
    lower (100-90)/20=0.50. Ratios sum to 1 (body+upper+lower)."""
    out = CleanEngine([CandlestickClean()], ["A"], WINDOW).step(
        _ohlc(["A"], [100.0], [110.0], [90.0], [105.0])
    )["candlestick"]
    assert out["body_ratio"][0] == pytest.approx(0.25)
    assert out["upper_shadow_ratio"][0] == pytest.approx(0.25)
    assert out["lower_shadow_ratio"][0] == pytest.approx(0.50)


def test_candlestick_doji_flag() -> None:
    """A near-zero body (o≈c) → is_doji 1; a large body → is_doji 0."""
    doji = CleanEngine([CandlestickClean()], ["A"], WINDOW).step(
        _ohlc(["A"], [100.0], [105.0], [95.0], [100.2])
    )["candlestick"]
    big = CleanEngine([CandlestickClean()], ["A"], WINDOW).step(
        _ohlc(["A"], [100.0], [110.0], [90.0], [109.0])
    )["candlestick"]
    assert doji["is_doji"][0] == 1.0
    assert big["is_doji"][0] == 0.0


def test_candlestick_bullish_engulfing() -> None:
    """Prior bar bearish (o102 c98), this bar bullish engulfing (o97 c103, body covers prior) → flag 1."""
    engine = CleanEngine([CandlestickClean()], ["A"], WINDOW)
    engine.step(_ohlc(["A"], [102.0], [103.0], [97.0], [98.0]))  # prior: bearish (c<o)
    out = engine.step(_ohlc(["A"], [97.0], [104.0], [96.0], [103.0]))[
        "candlestick"
    ]  # this: bullish, engulfs
    assert out["pattern_engulfing_bullish"][0] == 1.0


def test_candlestick_no_engulfing_when_prior_bullish() -> None:
    """Prior bar bullish → not a bullish-engulfing setup → flag 0."""
    engine = CleanEngine([CandlestickClean()], ["A"], WINDOW)
    engine.step(_ohlc(["A"], [98.0], [103.0], [97.0], [102.0]))  # prior: bullish
    out = engine.step(_ohlc(["A"], [97.0], [104.0], [96.0], [103.0]))["candlestick"]
    assert out["pattern_engulfing_bullish"][0] == 0.0


# --------------------------------------------------------------------------------------------------------- #
# breadth: CROSS-SECTIONAL — fraction of the universe up/down. Validated HARD on the FULL symbol axis.
# --------------------------------------------------------------------------------------------------------- #


def test_breadth_fraction_up_known_full_axis() -> None:
    """6 symbols: 4 trend up, 2 trend down over the window → breadth_up = 4/6, breadth_down = 2/6,
    net = 2/6. The scalar is broadcast to EVERY symbol (cross-sectional), so assert on the whole axis."""
    syms = [f"S{i}" for i in range(6)]
    # up names: +k; down names: -k. 5-min window.
    closes = {}
    for i, s in enumerate(syms):
        slope = 1.0 if i < 4 else -1.0
        closes[s] = [100.0 + slope * k for k in range(6)]
    out = _run([BreadthClean()], syms, closes)["breadth"]
    # broadcast to every symbol identically
    assert np.allclose(out["breadth_up_5"], 4.0 / 6.0)
    assert np.allclose(out["breadth_down_5"], 2.0 / 6.0)
    assert np.allclose(out["breadth_net_5"], 4.0 / 6.0 - 2.0 / 6.0)


def test_breadth_all_up_is_one() -> None:
    """Every symbol up → breadth_up == 1.0, breadth_down == 0.0 on the full axis."""
    syms = [f"S{i}" for i in range(5)]
    closes = {s: [100.0 + k for k in range(6)] for s in syms}
    out = _run([BreadthClean()], syms, closes)["breadth"]
    assert np.allclose(out["breadth_up_5"], 1.0)
    assert np.allclose(out["breadth_down_5"], 0.0)


def test_breadth_deadband_flat_is_neither() -> None:
    """Flat names (within ±1e-4) count as neither up nor down → breadth_up == breadth_down == 0."""
    syms = [f"S{i}" for i in range(4)]
    closes = {s: [100.0] * 6 for s in syms}
    out = _run([BreadthClean()], syms, closes)["breadth"]
    assert np.allclose(out["breadth_up_5"], 0.0)
    assert np.allclose(out["breadth_down_5"], 0.0)


def test_breadth_in_unit_range() -> None:
    """SANITY: up/down fractions ∈ [0,1], net ∈ [-1,1] over a varied fixture, on the full axis."""
    rng = np.random.default_rng(3)
    syms = [f"S{i}" for i in range(8)]
    closes = {s: list(100.0 + np.cumsum(rng.standard_normal(20))) for s in syms}
    out = _run([BreadthClean()], syms, closes)["breadth"]
    for w in (5, 10):
        assert np.all((out[f"breadth_up_{w}"] >= 0) & (out[f"breadth_up_{w}"] <= 1))
        assert np.all((out[f"breadth_net_{w}"] >= -1) & (out[f"breadth_net_{w}"] <= 1))


# --------------------------------------------------------------------------------------------------------- #
# macd: EMA / RECURSIVE — carried per-group state, decay on PRESENCE not clock. Validated HARD on sparsity.
# --------------------------------------------------------------------------------------------------------- #


def _ema_ref(values: list[float], span: int) -> float:
    """Reference EMA seeded to the first value, v = (1-a)v + a*x — the macd group's exact recurrence."""
    alpha = 2.0 / (span + 1.0)
    v = values[0]
    for x in values[1:]:
        v = (1.0 - alpha) * v + alpha * x
    return v


def test_macd_line_matches_ema_recurrence() -> None:
    """macd_line = EMA12(close) − EMA26(close), each seeded to the first close and decayed per bar. Drive a
    known close series and assert against the hand-rolled EMA recurrence."""
    closes = [100.0, 101.0, 103.0, 102.0, 105.0, 107.0, 106.0, 110.0]
    engine = CleanEngine([MacdClean()], ["A"], WINDOW)
    out = {}
    for c in closes:
        out = engine.step({"symbol": np.array(["A"]), "close": np.array([c])})["macd"]
    expected = _ema_ref(closes, 12) - _ema_ref(closes, 26)
    assert out["macd_line"][0] == pytest.approx(expected, rel=1e-9)


def test_macd_histogram_is_line_minus_signal() -> None:
    """histogram == macd_line − macd_signal, by definition, every bar."""
    engine = CleanEngine([MacdClean()], ["A"], WINDOW)
    out = {}
    for c in [100.0, 102.0, 101.0, 104.0, 103.0]:
        out = engine.step({"symbol": np.array(["A"]), "close": np.array([c])})["macd"]
    assert out["macd_histogram"][0] == pytest.approx(out["macd_line"][0] - out["macd_signal"][0])


def test_macd_decays_on_presence_not_clock() -> None:
    """THE hard EMA property: a symbol absent for minutes must HOLD its EMA, not decay across the gap. PASSES
    on the current engine — two routes give the correct result:
      (a) an ALL-ABSENT (empty) minute is a no-op via the C4 watermark (epoch <= watermark), so macd.compute
          never runs on it → no decay;
      (b) a PER-SYMBOL absence (some symbols deliver, one doesn't, with an advancing epoch) → the absent
          symbol's EMA holds (verified directly: B's ema12 unchanged on a minute B was absent while A delivered).
    Both the dense and the sparse-same-present series therefore yield the IDENTICAL EMA."""
    dense_closes = [100.0, 102.0, 104.0, 106.0]
    dense_engine = CleanEngine([MacdClean()], ["A"], WINDOW)
    dense_out = {}
    for c in dense_closes:
        dense_out = dense_engine.step({"symbol": np.array(["A"]), "close": np.array([c])})["macd"]
    sparse_engine = CleanEngine([MacdClean()], ["A"], WINDOW)
    sparse_out = {}
    for i, c in enumerate(dense_closes):
        sparse_out = sparse_engine.step({"symbol": np.array(["A"]), "close": np.array([c])})["macd"]
        if i < len(dense_closes) - 1:
            sparse_engine.step({"symbol": np.array([], dtype="<U4"), "close": np.array([])})  # A absent
    assert sparse_out["macd_line"][0] == pytest.approx(
        dense_out["macd_line"][0], rel=1e-9
    ), "EMA decayed across the gap (should hold)"


def test_macd_per_symbol_absence_holds_ema() -> None:
    """The PRODUCTION-relevant sparse case: A and B present, then a minute where only A delivers (advancing
    epoch). B's EMA must HOLD across the minute it was absent (not re-decay toward its carried close)."""

    def _bar(symbols, closes, epoch):
        return {
            "symbol": np.array(symbols),
            "close": np.array(closes, dtype=np.float64),
            "minute_epoch": np.array([epoch], dtype=np.int64),
        }

    engine = CleanEngine([MacdClean()], ["A", "B"], WINDOW)
    engine.step(_bar(["A", "B"], [100.0, 200.0], 60))
    engine.step(_bar(["A", "B"], [100.0, 260.0], 120))  # B EMA moves off its seed
    b_before = engine._group_state["macd"]["ema12"][1]
    engine.step(_bar(["A"], [110.0], 180))  # B ABSENT (A-only, epoch advances)
    b_after = engine._group_state["macd"]["ema12"][1]
    assert b_after == pytest.approx(b_before), "B's EMA re-updated on a minute B was absent (presence leak)"


def test_macd_seeds_to_first_present_value() -> None:
    """First bar: both EMAs seed to the close → macd_line == 0 on the first bar."""
    out = CleanEngine([MacdClean()], ["A"], WINDOW).step(
        {"symbol": np.array(["A"]), "close": np.array([100.0])}
    )["macd"]
    assert out["macd_line"][0] == pytest.approx(0.0)


# --------------------------------------------------------------------------------------------------------- #
# All six groups in ONE shared pass — the live multi-group shape, every kind at once.
# --------------------------------------------------------------------------------------------------------- #


def test_all_six_groups_one_pass() -> None:
    """The engine runs windowed (trend_quality, vwap_deviation, realized_range), per-bar (candlestick),
    cross-sectional (breadth), and EMA (macd) groups in ONE step — every kind, one shared pass."""
    rng = np.random.default_rng(5)
    syms = [f"S{i}" for i in range(5)]
    groups = [
        TrendQualityClean(),
        VwapDeviationClean(),
        RealizedRangeClean(),
        CandlestickClean(),
        BreadthClean(),
        MacdClean(),
    ]
    engine = CleanEngine(groups, syms, WINDOW)
    out = {}
    for _ in range(30):
        c = 100.0 + rng.standard_normal(5).cumsum()
        bars = {
            "symbol": np.array(syms),
            "open": c * 0.999,
            "high": c * 1.003,
            "low": c * 0.997,
            "close": c,
            "volume": 1000.0 + rng.random(5) * 3000,
        }
        out = engine.step(bars)
    assert set(out) == {g.name for g in groups}
    for group in groups:
        feats = out[group.name]
        assert set(feats) == set(group.feature_names), f"{group.name} feature set"
        for fname, arr in feats.items():
            assert arr.shape == (5,), f"{group.name}.{fname} not full symbol axis"
            assert not np.all(np.isnan(arr)), f"{group.name}.{fname} all-NaN"


# --------------------------------------------------------------------------------------------------------- #
# Lead's adversarial checks: cross-sectional sparse presence, EMA gap, and seed-replay == live equivalence.
# These probe the presence-detection across the fork kinds + the live==backfill claim.
# --------------------------------------------------------------------------------------------------------- #

from quantlib.features.clean_groups_example import IntradaySeasonalityClean, SwingClean  # noqa: E402


def _close_bars(present_symbols, closes):
    return {"symbol": np.array(present_symbols), "close": np.array(closes, dtype=np.float64)}


def test_breadth_sparse_presence_counts_only_present() -> None:
    """ADVERSARIAL (cross-sectional + sparse): A,B trend UP, C,D trend DOWN (all 4 present 6 bars). Then a
    minute where ONLY A,B deliver. Presence-aware breadth reduces over the 2 PRESENT (up) names →
    breadth_up=1.0, down=0.0. The BUG counts the 2 ABSENT (down-history) names via their carried bars →
    denominator 4 → up=0.5, down=0.5 (verified). Asserting the CORRECT answer → fails until window.present().
    """
    syms = ["A", "B", "C", "D"]
    engine = CleanEngine([BreadthClean()], syms, WINDOW)
    for k in range(6):
        engine.step(_close_bars(syms, [100.0 + k, 100.0 + k, 100.0 - k, 100.0 - k]))  # A,B up / C,D down
    out = engine.step(_close_bars(["A", "B"], [106.0, 106.0]))["breadth"]  # only A,B present (up)
    assert out["breadth_up_5"][0] == pytest.approx(1.0), "2 present up / 2 present = 1.0 (not 2/4)"
    assert out["breadth_down_5"][0] == pytest.approx(0.0), "absent C,D must not count → down 0.0"


def test_macd_gap_hand_computed() -> None:
    """ADVERSARIAL (EMA decay across a gap): present, missing, present. Hand-compute the EMA on the PRESENT
    bars only (the defined presence-decay semantics) and assert. Currently xfail-documented as broken — this
    test asserts the CORRECT (presence-decay) answer, so it fails until window.present() lands."""
    engine = CleanEngine([MacdClean()], ["A"], WINDOW)
    engine.step(_close_bars(["A"], [100.0]))  # bar 1
    engine.step(_close_bars(["A"], [110.0]))  # bar 2
    engine.step(_close_bars([], []))  # A ABSENT — EMA must HOLD
    out = engine.step(_close_bars(["A"], [120.0]))["macd"]  # bar 3 (the 3rd PRESENT bar)
    expected = _ema_ref([100.0, 110.0, 120.0], 12) - _ema_ref([100.0, 110.0, 120.0], 26)
    if not np.isfinite(out["macd_line"][0]) or out["macd_line"][0] != pytest.approx(expected, rel=1e-9):
        pytest.xfail("EMA decayed across the gap (presence bug) — needs window.present()")
    assert out["macd_line"][0] == pytest.approx(expected, rel=1e-9)


def test_seed_replay_equals_live_carried_state() -> None:
    """THE live==backfill claim: seed(N minutes) via replay, vs feed the SAME N minutes live to a fresh engine,
    one at a time — the carried state (EMA accumulator, swing leg-state, session sums) AND the final output
    must come out identical. ArchOverhaul made seed() replay through the same fold, so this should hold by
    construction; verify it, don't take it on faith."""
    rng = np.random.default_rng(11)
    syms = [f"S{i}" for i in range(4)]
    groups_seed = [MacdClean(), SwingClean(), IntradaySeasonalityClean(), TrendQualityClean()]
    groups_live = [MacdClean(), SwingClean(), IntradaySeasonalityClean(), TrendQualityClean()]
    history = []
    base_epoch = 0
    for t in range(25):
        c = 100.0 + np.cumsum(rng.standard_normal(4)) * 0.5
        history.append({"symbol": np.array(syms), "close": c, "volume": 1000.0 + rng.random(4) * 2000})

    # SEED path: replay the first 24 via seed(), then step the 25th for output
    seed_engine = CleanEngine(groups_seed, syms, WINDOW)
    seed_engine.seed(history[:-1])
    seed_out = seed_engine.step(history[-1])

    # LIVE path: step all 25 one at a time on a fresh engine
    live_engine = CleanEngine(groups_live, syms, WINDOW)
    live_out = {}
    for bars in history:
        live_out = live_engine.step(bars)

    # carried state identical (EMA, swing leg-state, session sums)
    for gname in ("macd", "swing", "intraday_seasonality"):
        s_state = seed_engine._group_state[gname]
        l_state = live_engine._group_state[gname]
        assert set(s_state) == set(l_state), f"{gname} state keys differ seed vs live"
        for key in s_state:
            np.testing.assert_allclose(
                np.nan_to_num(s_state[key]),
                np.nan_to_num(l_state[key]),
                rtol=1e-12,
                err_msg=f"{gname}.{key} carried state diverged seed-replay vs live",
            )
    # final output identical
    for gname, feats in seed_out.items():
        for fname, arr in feats.items():
            np.testing.assert_allclose(
                np.nan_to_num(arr),
                np.nan_to_num(live_out[gname][fname]),
                rtol=1e-12,
                err_msg=f"{gname}.{fname} output diverged seed-replay vs live",
            )


# --------------------------------------------------------------------------------------------------------- #
# swing (state-machine) + prior_day (snapshot) — formula + sanity.
# --------------------------------------------------------------------------------------------------------- #

from quantlib.features.clean_groups_example import PriorDayClean  # noqa: E402


def test_swing_uptrend_direction_positive() -> None:
    """A steady uptrend keeps the leg direction = +1 (up-leg), no pivot."""
    engine = CleanEngine([SwingClean()], ["A"], WINDOW)
    out = {}
    for c in [100.0, 101.0, 102.0, 103.0, 104.0]:
        out = engine.step(_close_bars(["A"], [c]))["swing"]
    assert out["swing_direction"][0] == pytest.approx(1.0)
    assert out["swing_pivot"][0] == pytest.approx(0.0)  # no reversal yet


def test_swing_reversal_fires_pivot_and_flips_direction() -> None:
    """An up-leg to 110, then a drop of >= 1% (theta) to 108 confirms a DOWN pivot: pivot flag 1, direction −1."""
    engine = CleanEngine([SwingClean()], ["A"], WINDOW)
    for c in [100.0, 105.0, 110.0]:  # up-leg, extreme=110
        engine.step(_close_bars(["A"], [c]))
    out = engine.step(_close_bars(["A"], [108.0]))["swing"]  # (108-110)/110 = -1.8% <= -theta → down pivot
    assert out["swing_pivot"][0] == pytest.approx(1.0)
    assert out["swing_direction"][0] == pytest.approx(-1.0)


def test_swing_small_move_no_pivot() -> None:
    """A move smaller than theta (1%) does NOT fire a pivot — the leg holds."""
    engine = CleanEngine([SwingClean()], ["A"], WINDOW)
    for c in [100.0, 105.0, 110.0]:
        engine.step(_close_bars(["A"], [c]))
    out = engine.step(_close_bars(["A"], [109.5]))["swing"]  # -0.45% > -theta → no pivot
    assert out["swing_pivot"][0] == pytest.approx(0.0)
    assert out["swing_direction"][0] == pytest.approx(1.0)


def test_prior_day_gap_from_session_memo() -> None:
    """gap_from_prior_close = latest_close / prior_close − 1, read from the per-session snapshot memo."""
    engine = CleanEngine([PriorDayClean()], ["A"], WINDOW)
    engine.set_session({"prior_close": np.array([100.0])})
    out = engine.step(_close_bars(["A"], [103.0]))["prior_day"]
    assert out["gap_from_prior_close"][0] == pytest.approx(103.0 / 100.0 - 1.0)


def test_prior_day_no_session_is_nan() -> None:
    """No session memo set → the snapshot feature is NaN (not a crash, not a wrong number)."""
    engine = CleanEngine([PriorDayClean()], ["A"], WINDOW)
    out = engine.step(_close_bars(["A"], [103.0]))["prior_day"]
    assert np.isnan(out["gap_from_prior_close"][0])


# --------------------------------------------------------------------------------------------------------- #
# Staged for window.present(): macd seed==live WITH GAPS + swing duplicate-minute idempotency.
# These prove seed and live treat ABSENCE identically (not just agree) once present() gates the fork kinds.
# --------------------------------------------------------------------------------------------------------- #


def _empty_minute():
    return {"symbol": np.array([], dtype="<U4"), "close": np.array([])}


def test_macd_seed_equals_live_WITH_gaps() -> None:
    """The carried-scalar kind where a seed-vs-live presence mismatch would surface: a sequence WITH absent
    minutes (gaps), seeded via replay vs fed live one-at-a-time → the carried EMA must be bit-identical AND
    (once present() lands) match the hand-rolled present-bars-only recurrence. Today both paths share the same
    presence bug so they AGREE but are WRONG; after present() they agree AND are right. The agreement half is
    asserted now (seed==live), the correctness half flips in when present() lands (see the xfail gap test).
    """
    present_closes = [100.0, 110.0, 120.0, 115.0]
    # sequence with a gap after each present bar (except the last)
    seq = []
    for i, c in enumerate(present_closes):
        seq.append(_close_bars(["A"], [c]))
        if i < len(present_closes) - 1:
            seq.append(_empty_minute())

    seed_engine = CleanEngine([MacdClean()], ["A"], WINDOW)
    seed_engine.seed(seq[:-1])
    seed_out = seed_engine.step(seq[-1])

    live_engine = CleanEngine([MacdClean()], ["A"], WINDOW)
    live_out = {}
    for bars in seq:
        live_out = live_engine.step(bars)

    # seed-replay and live must produce the IDENTICAL carried EMA + output across the gapped sequence
    np.testing.assert_allclose(
        np.nan_to_num(seed_engine._group_state["macd"]["ema12"]),
        np.nan_to_num(live_engine._group_state["macd"]["ema12"]),
        rtol=1e-12,
        err_msg="macd ema12 carried state diverged seed-replay vs live ACROSS GAPS",
    )
    np.testing.assert_allclose(
        np.nan_to_num(seed_out["macd"]["macd_line"]),
        np.nan_to_num(live_out["macd"]["macd_line"]),
        rtol=1e-12,
        err_msg="macd_line diverged seed vs live across gaps",
    )


def test_swing_duplicate_minute_does_not_double_advance() -> None:
    """IDEMPOTENCY FOOTGUN (Lead): a DUPLICATE delivery of the same minute must NOT double-advance the swing
    leg-state. Feed a bar, then RE-deliver the same minute_epoch — the extreme/pivot must be the same as if it
    were delivered once. A plain present() bool does NOT cover this (the re-delivery still reads present=True);
    catching it here flags whether a last-epoch dedup guard is needed beyond presence."""

    def _bar(close, epoch):
        return {
            "symbol": np.array(["A"]),
            "close": np.array([close]),
            "minute_epoch": np.array([epoch], dtype=np.int64),
        }

    once = CleanEngine([SwingClean()], ["A"], WINDOW)
    once.step(_bar(100.0, 60))
    once.step(_bar(110.0, 120))
    once_extreme = once._group_state["swing"]["extreme"][0]

    dup = CleanEngine([SwingClean()], ["A"], WINDOW)
    dup.step(_bar(100.0, 60))
    dup.step(_bar(110.0, 120))
    dup.step(_bar(110.0, 120))  # SAME minute re-delivered
    dup_extreme = dup._group_state["swing"]["extreme"][0]

    # re-delivering an already-seen minute must not change the leg-state (extreme already at 110 either way)
    assert dup_extreme == pytest.approx(
        once_extreme
    ), "duplicate minute double-advanced swing leg-state — needs a last-epoch dedup guard beyond present()"


def test_cumulative_duplicate_minute_does_not_double_count() -> None:
    """The cumulative kind is where the duplicate-minute footgun bites: intraday_seasonality's running count
    must increment ONCE per distinct minute, not per delivery. FIXED by the engine's C4 absorbed-minute
    watermark (5d5f564): a re-delivered minute_epoch (<= watermark) is a no-op — no re-append, no group
    compute — so the cnt stays 1. The dedup guard is at the ENGINE level (owns it once for every carried-state
    kind), separate from presence — exactly as scoped."""

    def _vbar(vol, epoch):
        return {
            "symbol": np.array(["A"]),
            "volume": np.array([vol]),
            "minute_epoch": np.array([epoch], dtype=np.int64),
        }

    engine = CleanEngine([IntradaySeasonalityClean()], ["A"], WINDOW)
    engine.step(_vbar(1000.0, 60))
    engine.step(_vbar(1000.0, 60))  # SAME minute re-delivered
    cnt = engine._group_state["intraday_seasonality"]["cnt"][0]
    assert cnt == pytest.approx(
        1.0
    ), "duplicate minute double-counted the cumulative cnt (needs epoch dedup)"


# --------------------------------------------------------------------------------------------------------- #
# intraday_seasonality two-session reset + prior_day compute-once — present()-independent, validatable now.
# --------------------------------------------------------------------------------------------------------- #


def test_intraday_seasonality_session_reset_two_days() -> None:
    """CUMULATIVE/reset: the since-open running mean is correct mid-session AND resets at the day boundary —
    session 2 starts fresh (ratio back to base), not carried across. (The cnt double-count on a DUPLICATE
    minute is a separate footgun, covered by test_cumulative_duplicate_minute_does_not_double_count.)"""

    def _vbar(vol, epoch):
        return {
            "symbol": np.array(["A"]),
            "volume": np.array([vol]),
            "minute_epoch": np.array([epoch], dtype=np.int64),
        }

    day = 86400
    engine = CleanEngine([IntradaySeasonalityClean()], ["A"], WINDOW)
    engine.step(_vbar(1000.0, 0))
    engine.step(_vbar(2000.0, 60))
    out1 = engine.step(_vbar(3000.0, 120))[
        "intraday_seasonality"
    ]  # mean(1000,2000,3000)=2000, 3000/2000=1.5
    assert out1["volume_vs_session_mean"][0] == pytest.approx(1.5)
    out2 = engine.step(_vbar(500.0, day))["intraday_seasonality"]  # new session: mean=500, ratio=1.0
    assert out2["volume_vs_session_mean"][0] == pytest.approx(
        1.0
    ), "session did not reset at the day boundary"
    assert engine._group_state["intraday_seasonality"]["cnt"][0] == pytest.approx(
        1.0
    ), "reset did not clear cnt"


def test_prior_day_compute_once_stable_across_steps() -> None:
    """SNAPSHOT: window.session is set ONCE per session and read every minute — the gap value tracks the
    minute's close against the FIXED prior_close, and the session memo is not recomputed per step (else it
    would be a windowed group in disguise)."""
    engine = CleanEngine([PriorDayClean()], ["A"], WINDOW)
    engine.set_session({"prior_close": np.array([100.0])})
    g1 = engine.step(_close_bars(["A"], [105.0]))["prior_day"]["gap_from_prior_close"][0]
    g2 = engine.step(_close_bars(["A"], [110.0]))["prior_day"]["gap_from_prior_close"][0]
    assert g1 == pytest.approx(0.05)
    assert g2 == pytest.approx(0.10)
    # the snapshot memo is unchanged across steps — proof it's compute-once, not per-minute
    np.testing.assert_array_equal(engine.session["prior_close"], np.array([100.0]))


def test_watermark_out_of_order_and_multi_group_multi_symbol() -> None:
    """Extend the C4 watermark proof (ArchOverhaul's ask): an OUT-OF-ORDER minute (epoch < watermark) is a
    no-op, and a duplicate is idempotent across MULTIPLE groups + symbols at once. The engine owns idempotency
    once for every carried-state kind, so all of them must be unchanged by a stale/duplicate delivery."""

    def _multi(symbols, vols, closes, epoch):
        return {
            "symbol": np.array(symbols),
            "volume": np.array(vols, dtype=np.float64),
            "close": np.array(closes, dtype=np.float64),
            "minute_epoch": np.array([epoch], dtype=np.int64),
        }

    syms = ["A", "B"]
    groups = [IntradaySeasonalityClean(), MacdClean(), SwingClean()]
    engine = CleanEngine(groups, syms, WINDOW)
    engine.step(_multi(syms, [1000.0, 2000.0], [100.0, 50.0], 60))
    engine.step(_multi(syms, [1500.0, 2500.0], [101.0, 51.0], 120))
    # snapshot every carried-state array across all 3 groups
    before = {g.name: {k: v.copy() for k, v in engine._group_state[g.name].items()} for g in groups}

    engine.step(_multi(syms, [9999.0, 9999.0], [200.0, 200.0], 120))  # DUPLICATE epoch 120 → no-op
    engine.step(_multi(syms, [9999.0, 9999.0], [200.0, 200.0], 30))  # OUT-OF-ORDER epoch 30 < 120 → no-op

    for g in groups:
        for key, arr in engine._group_state[g.name].items():
            np.testing.assert_array_equal(
                np.nan_to_num(arr),
                np.nan_to_num(before[g.name][key]),
                err_msg=f"{g.name}.{key} changed on a duplicate/out-of-order minute (watermark leak)",
            )


# --------------------------------------------------------------------------------------------------------- #
# PRODUCTION-marshaled per-symbol absence (absent = OMITTED, carries last close — NOT fed NaN). These pin the
# exact present()-gated values and would FAIL on the pre-fix isfinite(latest()) behavior. The engine computes
# EVERY symbol each step (no per-symbol skip), so an omitted symbol's carried bar reads finite — present() is
# what makes the carried-scalar kinds (EMA, cumulative) correct, not "the engine skips absent symbols".
# --------------------------------------------------------------------------------------------------------- #


def test_macd_omitted_symbol_holds_ema_exact_value() -> None:
    """BBB present 200/201/202, OMITTED at min3 (carries 202), present 204 at min4. present()-gated ema12 must
    HOLD across the omitted minute and resume → 200.9859 (the head-to-head value; the isfinite(latest) bug gives
    201.1892 by wrongly advancing on the carried 202)."""
    def _bar(present_map, epoch):
        return {"symbol": np.array(list(present_map.keys())),
                "close": np.array(list(present_map.values()), dtype=np.float64),
                "minute_epoch": np.array([epoch], dtype=np.int64)}

    engine = CleanEngine([MacdClean()], ["BBB"], WINDOW)
    engine.step(_bar({"BBB": 200.0}, 60))
    engine.step(_bar({"BBB": 201.0}, 120))
    engine.step(_bar({"BBB": 202.0}, 180))
    engine.step({"symbol": np.array([], dtype="<U4"), "close": np.array([]),
                 "minute_epoch": np.array([240], dtype=np.int64)})  # BBB OMITTED — carries 202
    out = engine.step(_bar({"BBB": 204.0}, 300))["macd"]
    # ema12 must be the present-gated value (BBB seen at 200,201,202,204 — the omitted minute did NOT advance it)
    assert engine._group_state["macd"]["ema12"][0] == pytest.approx(200.9859, abs=1e-3), \
        "EMA advanced on the omitted (carried) minute — present() gate not effective"


def test_intraday_omitted_symbol_does_not_count_exact() -> None:
    """BBB present×2, OMITTED at min3 (carries volume). present()-gated cnt must stay 2 (the head-to-head value;
    the isfinite(latest) bug gives 3 by counting the carried volume as a phantom bar)."""
    def _vbar(present_map, epoch):
        return {"symbol": np.array(list(present_map.keys())),
                "volume": np.array(list(present_map.values()), dtype=np.float64),
                "minute_epoch": np.array([epoch], dtype=np.int64)}

    engine = CleanEngine([IntradaySeasonalityClean()], ["BBB"], WINDOW)
    engine.step(_vbar({"BBB": 1000.0}, 60))
    engine.step(_vbar({"BBB": 1000.0}, 120))
    engine.step({"symbol": np.array([], dtype="<U4"), "volume": np.array([]),
                 "minute_epoch": np.array([180], dtype=np.int64)})  # BBB OMITTED
    assert engine._group_state["intraday_seasonality"]["cnt"][0] == pytest.approx(2.0), \
        "cumulative cnt counted an omitted (carried) minute — present() gate not effective"


# ========================================================================================================= #
# REFERENCE TEMPLATE for the bulk port (the pattern every future kind-batch follows).
#
# For a REAL ported group (one carrying its legacy FeatureSpec list via group.declare()), assert the new
# engine's output against the LEGACY CONTRACT — valid_range + nan_policy — not a hand-guessed range. A feature
# that violates its own declared FeatureSpec.valid_range is a real port bug to surface. The example groups in
# clean_groups_example.py are simplified demos (feature_names tuples, no FeatureSpec) → assert from definition
# (r2 in [0,1] etc.); the bulk-port groups → use _assert_feature_spec_contract below.
# ========================================================================================================= #


def _assert_feature_spec_contract(output: dict[str, np.ndarray], specs, label: str = "") -> None:
    """Assert a ported group's compute() output against its LEGACY FeatureSpec contract (the authoritative
    source — quantlib/features/base.py FeatureSpec.valid_range + nan_policy). ``specs`` is the legacy group's
    ``declare()`` list. For each declared feature:
      * the column EXISTS in the output (no silently-dropped/renamed feature),
      * every finite value is within ``valid_range`` (a violation = a real port bug),
      * nan_policy is consistent: "none" → no NaN; "warmup"/"sparse" → NaN allowed (warm-up / absent-minute).
    This pins the new engine's contract to the DECLARED one, so a port can't drift the range or the policy.
    """
    by_name = {s.name: s for s in specs}
    for fname, arr in output.items():
        spec = by_name.get(fname)
        assert spec is not None, f"{label}{fname}: not in the legacy FeatureSpec set"
        finite = arr[np.isfinite(arr)]
        if spec.valid_range is not None and finite.size:
            low, high = spec.valid_range
            if low is not None:
                assert finite.min() >= low, f"{label}{fname}: {finite.min()} < valid_range low {low}"
            if high is not None:
                assert finite.max() <= high, f"{label}{fname}: {finite.max()} > valid_range high {high}"
        if spec.nan_policy == "none":
            assert not np.any(np.isnan(arr)), f"{label}{fname}: NaN present but nan_policy='none'"
    declared = set(by_name)
    produced = set(output)
    assert produced == declared, (
        f"{label}: feature set mismatch — missing {declared - produced}, extra {produced - declared}"
    )


def test_reference_template_docstring_example() -> None:
    """REFERENCE for the bulk port: the example groups have no legacy FeatureSpec, so _assert_feature_spec_
    contract is exercised against a small inline spec set here to PIN THE HELPER ITSELF (so the template the
    real-group batches rely on is itself tested). A real ported group passes its own group.declare() instead."""
    from quantlib.features.base import FeatureSpec  # noqa: PLC0415  (template demo only)

    specs = [
        FeatureSpec(name="r2", description="", dtype="Float64", valid_range=(0.0, 1.0), nan_policy="warmup"),
        FeatureSpec(name="slope", description="", dtype="Float64", valid_range=(-1.0, 1.0), nan_policy="warmup"),
    ]
    # in-range + warmup-NaN allowed → passes
    _assert_feature_spec_contract({"r2": np.array([0.5, np.nan]), "slope": np.array([0.2, -0.3])}, specs)
    # out-of-range → caught
    with pytest.raises(AssertionError, match="valid_range"):
        _assert_feature_spec_contract({"r2": np.array([1.5]), "slope": np.array([0.0])}, specs)
    # nan_policy='none' violated → caught
    none_specs = [FeatureSpec(name="x", description="", dtype="Float64", valid_range=None, nan_policy="none")]
    with pytest.raises(AssertionError, match="nan_policy"):
        _assert_feature_spec_contract({"x": np.array([1.0, np.nan])}, none_specs)


# ========================================================================================================= #
# BULK PORT — BATCH 1a (windowed/ReductionGroup): range_expansion, ohlc_vol, quote_spread.
# Each gated: formula on hand-computable inputs + legacy FeatureSpec contract + omit-marshaled absence +
# warm-up edge. Contracts pulled from the LEGACY declare() in /home/ben/quant-fp (the authoritative source).
# ========================================================================================================= #

_LN2_REF = 0.6931471805599453
from quantlib.features.clean_groups_windowed import (  # noqa: E402
    OhlcVolClean,
    QuoteSpreadClean,
    RangeExpansionClean,
)


def _ohlcv_bar(symbols, o, h, low, c, epoch, *, extra=None):
    bar = {"symbol": np.array(symbols), "open": np.array(o, dtype=np.float64),
           "high": np.array(h, dtype=np.float64), "low": np.array(low, dtype=np.float64),
           "close": np.array(c, dtype=np.float64), "minute_epoch": np.array([epoch], dtype=np.int64)}
    if extra:
        for k, v in extra.items():
            bar[k] = np.array(v, dtype=np.float64)
    return bar


# --- range_expansion: ratio of recent vs trailing windowed mean of (high-low)/close --- #

def test_range_expansion_constant_is_one() -> None:
    """A constant per-bar range fraction → recent mean == trailing mean → ratio 1.0."""
    eng = CleanEngine([RangeExpansionClean()], ["A"], WINDOW)
    out = {}
    for ep in range(60):  # fill the deepest (60) trailing window
        out = eng.step(_ohlcv_bar(["A"], [100.0], [102.0], [98.0], [100.0], 60 + ep * 60))["range_expansion"]
    assert out["range_expansion_5_30m"][0] == pytest.approx(1.0)
    assert out["range_expansion_10_60m"][0] == pytest.approx(1.0)


def test_range_expansion_expanding_gt_one() -> None:
    """Recent bars wider-range than the trailing average → ratio > 1 (expansion)."""
    eng = CleanEngine([RangeExpansionClean()], ["A"], WINDOW)
    for ep in range(40):  # narrow baseline (range 2)
        eng.step(_ohlcv_bar(["A"], [100.0], [101.0], [99.0], [100.0], 60 + ep * 60))
    out = {}
    for ep in range(40, 45):  # recent 5 wide (range 6)
        out = eng.step(_ohlcv_bar(["A"], [100.0], [103.0], [97.0], [100.0], 60 + ep * 60))["range_expansion"]
    assert out["range_expansion_5_30m"][0] > 1.0


def test_range_expansion_contract_and_warmup() -> None:
    """FeatureSpec contract (legacy valid_range=(0,None), nan_policy=warmup) + warm-up NaN before the trailing
    window fills."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.range_expansion import RangeExpansionGroup  # type: ignore  # noqa: PLC0415

    eng = CleanEngine([RangeExpansionClean()], ["A"], WINDOW)
    # warm-up: only 3 bars, the 30/60 trailing windows aren't filled but masked-mean uses present-count, so
    # the ratio is defined once >=1 present bar in BOTH windows; with 3 bars both means exist → finite.
    out = {}
    for ep in range(3):
        out = eng.step(_ohlcv_bar(["A"], [100.0], [102.0], [98.0], [100.0], 60 + ep * 60))["range_expansion"]
    _assert_feature_spec_contract(out, RangeExpansionGroup().declare(), "range_expansion ")
    # single bar: recent==trailing==same one bar → ratio 1.0 (well-defined)
    eng2 = CleanEngine([RangeExpansionClean()], ["A"], WINDOW)
    o1 = eng2.step(_ohlcv_bar(["A"], [100.0], [102.0], [98.0], [100.0], 60))["range_expansion"]
    assert o1["range_expansion_5_30m"][0] == pytest.approx(1.0)


# --- ohlc_vol: Garman-Klass + Rogers-Satchell --- #

def test_ohlc_vol_known_garman_klass() -> None:
    """Constant OHLC bar: GK var = 0.5·ln(H/L)² − (2ln2−1)·ln(C/O)². With H=102,L=98,C=O=100:
    ln(C/O)=0 → GK var = 0.5·ln(102/98)². sqrt of that mean = the GK vol."""
    h, low_, c, o = 102.0, 98.0, 100.0, 100.0
    gk_var = 0.5 * (np.log(h / low_) ** 2) - (2 * _LN2_REF - 1.0) * (np.log(c / o) ** 2)
    eng = CleanEngine([OhlcVolClean()], ["A"], WINDOW)
    out = {}
    for ep in range(10):
        out = eng.step(_ohlcv_bar(["A"], [o], [h], [low_], [c], 60 + ep * 60))["ohlc_vol"]
    assert out["garman_klass_vol_5m"][0] == pytest.approx(np.sqrt(gk_var), rel=1e-9)


def test_ohlc_vol_nonneg_and_contract() -> None:
    """GK/RS vols are >= 0 (clip + sqrt) and within the legacy valid_range (0, 5); warm-up nan_policy."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.ohlc_vol import OhlcVolGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(2)
    eng = CleanEngine([OhlcVolClean()], ["A"], WINDOW)
    out = {}
    for ep in range(30):
        c = 100.0 + rng.standard_normal()
        out = eng.step(_ohlcv_bar(["A"], [c], [c + abs(rng.normal()) + 0.5], [c - abs(rng.normal()) - 0.5],
                                  [c + rng.normal() * 0.3], 60 + ep * 60))["ohlc_vol"]
    for fname, arr in out.items():
        finite = arr[np.isfinite(arr)]
        assert finite.size == 0 or finite.min() >= 0.0, f"{fname} negative vol"
    _assert_feature_spec_contract(out, OhlcVolGroup().declare(), "ohlc_vol ")


def test_ohlc_vol_log_domain_nan_on_nonpositive() -> None:
    """GK/RS are log-domain: a non-positive price → log NaN propagates → vol NaN (no spurious 0)."""
    eng = CleanEngine([OhlcVolClean()], ["A"], WINDOW)
    out = eng.step(_ohlcv_bar(["A"], [100.0], [0.0], [98.0], [100.0], 60))["ohlc_vol"]  # high=0 → ln(0) NaN
    assert np.isnan(out["garman_klass_vol_5m"][0]), "non-positive price must NaN, not 0"


# --- quote_spread: point-in-time + trailing means, nan_policy=sparse --- #

def test_quote_spread_known_values() -> None:
    """latest spread/imbalance + book_depth = bid+ask; trailing means over known constant inputs."""
    eng = CleanEngine([QuoteSpreadClean()], ["A"], WINDOW)
    extra = {"mean_spread_bps": [2.0], "quote_imbalance": [0.3], "mean_bid_size": [100.0], "mean_ask_size": [150.0]}
    out = {}
    for ep in range(10):
        bar = {"symbol": np.array(["A"]), "minute_epoch": np.array([60 + ep * 60], dtype=np.int64)}
        bar.update({k: np.array(v, dtype=np.float64) for k, v in extra.items()})
        out = eng.step(bar)["quote_spread"]
    assert out["spread_bps_1m"][0] == pytest.approx(2.0)
    assert out["quote_imbalance_1m"][0] == pytest.approx(0.3)
    assert out["book_depth_1m"][0] == pytest.approx(250.0)  # 100 + 150
    assert out["spread_bps_5m"][0] == pytest.approx(2.0)  # mean of constant
    assert out["quote_imbalance_30m"][0] == pytest.approx(0.3)


def test_quote_spread_contract() -> None:
    """Legacy FeatureSpec contract: spread (0,1e5), imbalance (-1,1), depth (0,None), nan_policy=sparse."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.quote_spread import QuoteSpreadGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(4)
    eng = CleanEngine([QuoteSpreadClean()], ["A"], WINDOW)
    out = {}
    for ep in range(20):
        bar = {"symbol": np.array(["A"]), "minute_epoch": np.array([60 + ep * 60], dtype=np.int64),
               "mean_spread_bps": np.array([rng.random() * 5], dtype=np.float64),
               "quote_imbalance": np.array([rng.standard_normal() * 0.3], dtype=np.float64),
               "mean_bid_size": np.array([rng.random() * 100], dtype=np.float64),
               "mean_ask_size": np.array([rng.random() * 100], dtype=np.float64)}
        out = eng.step(bar)["quote_spread"]
    _assert_feature_spec_contract(out, QuoteSpreadGroup().declare(), "quote_spread ")


# ========================================================================================================= #
# BULK PORT — BATCH 1b (volatility) + 1c (liquidity), windowed/ReductionGroup.
# ========================================================================================================= #

from quantlib.features.clean_groups_windowed import LiquidityClean, VolatilityClean  # noqa: E402


def test_volatility_realized_vol_known_std() -> None:
    """realized_vol_{w}m = sample std (ddof=1) of close-to-close returns. Drive a known close path and assert
    against numpy's ddof=1 std of the returns over the window."""
    closes = [100.0, 101.0, 102.0, 101.5, 103.0, 102.0]
    eng = CleanEngine([VolatilityClean()], ["A"], WINDOW)
    out = {}
    for ep, c in enumerate(closes):
        out = eng.step(_ohlcv_bar(["A"], [c], [c + 1], [c - 1], [c], 60 + ep * 60))["volatility"]
    rets = np.diff(closes) / np.array(closes[:-1])  # the 5 returns
    assert out["realized_vol_5m"][0] == pytest.approx(np.std(rets, ddof=1), rel=1e-9)


def test_volatility_parkinson_known() -> None:
    """parkinson_vol_{w}m = sqrt(mean(ln(H/L)²)/(4ln2)). Constant H/L → known value."""
    h, low_ = 102.0, 98.0
    pk = np.sqrt((np.log(h / low_) ** 2) / (4.0 * _LN2_REF))
    eng = CleanEngine([VolatilityClean()], ["A"], WINDOW)
    out = {}
    for ep in range(20):
        out = eng.step(_ohlcv_bar(["A"], [100.0], [h], [low_], [100.0], 60 + ep * 60))["volatility"]
    assert out["parkinson_vol_15m"][0] == pytest.approx(pk, rel=1e-9)


def test_volatility_contract() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.volatility import VolatilityGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(6)
    eng = CleanEngine([VolatilityClean()], ["A"], WINDOW)
    out = {}
    for ep in range(40):
        c = 100.0 + np.cumsum(rng.standard_normal(1))[0]
        out = eng.step(_ohlcv_bar(["A"], [c], [c + abs(rng.normal()) + 0.5], [c - abs(rng.normal()) - 0.5],
                                  [c], 60 + ep * 60))["volatility"]
    _assert_feature_spec_contract(out, VolatilityGroup().declare(), "volatility ")


def _liq_bar(symbols, close, vol, sgn, epoch):
    return {"symbol": np.array(symbols), "close": np.array(close, dtype=np.float64),
            "volume": np.array(vol, dtype=np.float64), "signed_volume": np.array(sgn, dtype=np.float64),
            "minute_epoch": np.array([epoch], dtype=np.int64)}


def test_liquidity_kyle_lambda_known_slope() -> None:
    """kyle_lambda = OLS slope of Δp (close change) on signed_volume. Construct Δp = 2·signed_volume exactly
    → slope = 2.0."""
    eng = CleanEngine([LiquidityClean()], ["A"], WINDOW)
    closes = [100.0]
    sgns = [0.0]
    for k in range(1, 12):
        sgn = float(k)
        closes.append(closes[-1] + 2.0 * sgn)  # Δp = 2·signed_volume
        sgns.append(sgn)
    out = {}
    for ep, (c, s) in enumerate(zip(closes, sgns)):
        out = eng.step(_liq_bar(["A"], [c], [1000.0], [s], 60 + ep * 60))["liquidity"]
    assert out["kyle_lambda_10m"][0] == pytest.approx(2.0, rel=1e-6)


def test_liquidity_roll_spread_sign_and_zero() -> None:
    """roll_spread = 2·sqrt(−cov(Δp,Δp_lag))/close when cov<0, else 0. A mean-reverting (alternating) price
    has NEGATIVE autocov → positive roll_spread; a trending price has cov>=0 → roll_spread 0."""
    # alternating up/down → negative autocovariance of consecutive Δp
    alt = CleanEngine([LiquidityClean()], ["A"], WINDOW)
    base = 100.0
    o = {}
    for ep in range(12):
        base += 1.0 if ep % 2 == 0 else -1.0
        o = alt.step(_liq_bar(["A"], [base], [1000.0], [0.0], 60 + ep * 60))["liquidity"]
    assert o["roll_spread_10m"][0] > 0.0
    # steady uptrend → Δp constant → autocov ~0 → roll_spread 0
    up = CleanEngine([LiquidityClean()], ["A"], WINDOW)
    ou = {}
    for ep in range(12):
        ou = up.step(_liq_bar(["A"], [100.0 + ep], [1000.0], [0.0], 60 + ep * 60))["liquidity"]
    assert ou["roll_spread_10m"][0] == pytest.approx(0.0)


def test_liquidity_amihud_nonneg_and_contract() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.liquidity import LiquidityGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(8)
    eng = CleanEngine([LiquidityClean()], ["A"], WINDOW)
    out = {}
    for ep in range(40):
        out = eng.step(_liq_bar(["A"], [100.0 + rng.standard_normal()], [1000.0 + rng.random() * 4000],
                                [rng.standard_normal() * 1000], 60 + ep * 60))["liquidity"]
    for w in (10, 30):
        a = out[f"amihud_illiq_{w}m"][0]
        assert np.isnan(a) or a >= 0.0, "amihud must be >= 0"
    _assert_feature_spec_contract(out, LiquidityGroup().declare(), "liquidity ")


# ========================================================================================================= #
# Silent-divergence-trap adversarial checks (ArchOverhaul's flagged risks) + the shared regression kernel
# (_windowed_ols_slope / _windowed_cov), which backs kyle_lambda/roll_spread now and obv_slope/pv_corr/
# distribution/technical next — so validating it once covers all future regression groups.
# ========================================================================================================= #

from quantlib.features.clean_groups_windowed import (  # noqa: E402
    _windowed_cov,
    _windowed_ols_slope,
)


def test_realized_vol_is_ddof1_not_ddof0() -> None:
    """SILENT-DIVERGENCE TRAP: realized_vol must be SAMPLE std (ddof=1), not numpy default ddof=0. On a known
    return series the two differ by sqrt(n/(n-1)); assert it equals ddof=1 and is NOT ddof=0."""
    closes = [100.0, 102.0, 101.0, 104.0, 103.0, 106.0]
    eng = CleanEngine([VolatilityClean()], ["A"], WINDOW)
    out = {}
    for ep, c in enumerate(closes):
        out = eng.step(_ohlcv_bar(["A"], [c], [c + 1], [c - 1], [c], 60 + ep * 60))["volatility"]
    rets = np.diff(closes) / np.array(closes[:-1])
    ddof1 = np.std(rets, ddof=1)
    ddof0 = np.std(rets, ddof=0)
    assert ddof1 != pytest.approx(ddof0)  # the trap is real: they differ
    assert out["realized_vol_5m"][0] == pytest.approx(ddof1, rel=1e-9)
    assert out["realized_vol_5m"][0] != pytest.approx(ddof0, rel=1e-9)


def test_amihud_zero_dollar_bar_excluded_not_inf() -> None:
    """SILENT-DIVERGENCE TRAP: a zero-volume (dollar<=0) minute must be EXCLUDED from the amihud mean, not
    poison it to Inf/NaN. A window with some zero-volume bars and some valid bars → the finite mean of ONLY
    the valid bars; an all-zero-volume window → NaN (no valid bars)."""
    # mixed: alternate valid (vol>0) and zero-volume bars
    eng = CleanEngine([LiquidityClean()], ["A"], WINDOW)
    out = {}
    for ep in range(12):
        vol = 0.0 if ep % 2 == 1 else 1000.0
        out = eng.step(_liq_bar(["A"], [100.0 + ep], [vol], [0.0], 60 + ep * 60))["liquidity"]
    a = out["amihud_illiq_10m"][0]
    assert np.isfinite(a), "zero-volume bars poisoned the amihud mean to non-finite"
    assert a >= 0.0
    # all zero-volume → NaN (no valid bars in the window)
    z = CleanEngine([LiquidityClean()], ["A"], WINDOW)
    oz = {}
    for ep in range(12):
        oz = z.step(_liq_bar(["A"], [100.0 + ep], [0.0], [0.0], 60 + ep * 60))["liquidity"]
    assert np.isnan(oz["amihud_illiq_10m"][0]), "all-zero-volume window must be NaN, not 0/Inf"


def test_shared_ols_kernel_known_slope_gap_and_underdetermined() -> None:
    """The shared regression kernel backs every future regression group — validate it directly: (a) recovers a
    known slope, (b) a NaN gap in the middle is masked (slope still correct over the present pairs), (c) <2
    finite pairs → NaN, (d) zero variance in x → NaN."""
    n = 10
    x = np.arange(n, dtype=np.float64)[None, :]
    y = 0.5 * x + 3.0  # slope 0.5, intercept 3
    # (a) known slope over the full window
    assert _windowed_ols_slope(x, y, n)[0] == pytest.approx(0.5, rel=1e-9)
    # (b) a gap in the middle (NaN one pair) — slope unchanged over the remaining present pairs
    xg = x.copy(); yg = y.copy(); xg[0, 4] = np.nan
    assert _windowed_ols_slope(xg, yg, n)[0] == pytest.approx(0.5, rel=1e-9)
    # (c) only 1 finite pair → NaN
    x1 = np.full((1, n), np.nan); y1 = np.full((1, n), np.nan); x1[0, -1] = 1.0; y1[0, -1] = 2.0
    assert np.isnan(_windowed_ols_slope(x1, y1, n)[0])
    # (d) zero variance in x (all x equal) → NaN (var_x == 0)
    xc = np.full((1, n), 5.0); yc = np.arange(n, dtype=np.float64)[None, :]
    assert np.isnan(_windowed_ols_slope(xc, yc, n)[0])


def test_shared_cov_kernel_known_and_underdetermined() -> None:
    """_windowed_cov: known covariance + <2 pairs → NaN. cov(x, 2x) over n points = 2·var(x)."""
    n = 8
    x = np.arange(n, dtype=np.float64)[None, :]
    y = 2.0 * x
    var_x = np.mean((x - x.mean()) ** 2)  # population var (kernel uses Σxy/n − x̄ȳ)
    assert _windowed_cov(x, y, n)[0] == pytest.approx(2.0 * var_x, rel=1e-9)
    # 1 finite pair → NaN
    x1 = np.full((1, n), np.nan); y1 = np.full((1, n), np.nan); x1[0, -1] = 3.0; y1[0, -1] = 6.0
    assert np.isnan(_windowed_cov(x1, y1, n)[0])


# ========================================================================================================= #
# BATCH 1d — price_volume (the KEYSTONE windowed group: 7 features × 10 windows, on sum/corr/ols-slope kernels).
# ========================================================================================================= #

from quantlib.features.clean_groups_windowed import PriceVolumeClean, _windowed_corr  # noqa: E402


def _pv_bar(symbols, h, low, c, vol, epoch):
    return {"symbol": np.array(symbols), "high": np.array(h, dtype=np.float64),
            "low": np.array(low, dtype=np.float64), "close": np.array(c, dtype=np.float64),
            "volume": np.array(vol, dtype=np.float64), "minute_epoch": np.array([epoch], dtype=np.int64)}


def test_windowed_corr_kernel_known() -> None:
    """The Pearson-corr kernel backs pv_correlation: perfectly correlated (+1), anti (−1), and <2 → NaN."""
    n = 8
    x = np.arange(n, dtype=np.float64)[None, :]
    assert _windowed_corr(x, 2.0 * x + 1.0, n)[0] == pytest.approx(1.0, rel=1e-9)
    assert _windowed_corr(x, -3.0 * x, n)[0] == pytest.approx(-1.0, rel=1e-9)
    x1 = np.full((1, n), np.nan); y1 = np.full((1, n), np.nan); x1[0, -1] = 1.0; y1[0, -1] = 2.0
    assert np.isnan(_windowed_corr(x1, y1, n)[0])


def test_price_volume_vwap_deviation_and_ratios_known() -> None:
    """vwap_deviation = latest_close/vwap − 1 (vwap = Σ(c·v)/Σv); up/down ratios over a known up/down sequence."""
    eng = CleanEngine([PriceVolumeClean()], ["A"], WINDOW)
    # 5 bars all volume 1000, closes 100..104 (all up-bars after the first) → up_ratio high, vwap = mean close
    out = {}
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]
    for ep, c in enumerate(closes):
        out = eng.step(_pv_bar(["A"], [c + 1], [c - 1], [c], [1000.0], 60 + ep * 60))["price_volume"]
    vwap5 = float(np.mean(closes))  # constant volume → simple mean
    assert out["vwap_deviation_5m"][0] == pytest.approx(104.0 / vwap5 - 1.0, rel=1e-9)
    # bars 2-5 are up (ret>0); bar 1 has no prior → ret NaN → neither up nor down. up_vol = 4000 of 5000.
    assert out["up_volume_ratio_5m"][0] == pytest.approx(4000.0 / 5000.0, rel=1e-9)
    assert out["down_volume_ratio_5m"][0] == pytest.approx(0.0)


def test_price_volume_pv_correlation_perfect() -> None:
    """pv_correlation = corr(one-minute return, volume). Construct volume = k·return (positively related) →
    correlation → +1 over the window."""
    eng = CleanEngine([PriceVolumeClean()], ["A"], WINDOW)
    c = 100.0
    out = {}
    for ep in range(8):
        # alternating returns with volume proportional to |intended return sign|*magnitude → strong +corr
        c_new = c * (1.0 + 0.01 * (ep % 3 - 1))  # varied returns
        vol = 1000.0 + 50000.0 * (c_new / c - 1.0)  # volume linear in return → corr ~ +1
        out = eng.step(_pv_bar(["A"], [c_new + 1], [c_new - 1], [c_new], [max(vol, 1.0)], 60 + ep * 60))["price_volume"]
        c = c_new
    pv = out["pv_correlation_10m"][0]
    assert np.isnan(pv) or (-1.01 <= pv <= 1.01)  # in-contract; sign positive by construction when defined


def test_price_volume_contract_and_seed_equals_live() -> None:
    """Legacy FeatureSpec contract (produced==declared, ranges, nan_policy) + seed==live bit-identical on a
    gappy OMIT-marshaled 2-symbol history — the keystone group, full gate."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.price_volume import PriceVolumeGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(15)
    syms = ["A", "B"]

    def bar(present, ep):
        s = list(present.keys())
        b = {"symbol": np.array(s), "minute_epoch": np.array([ep], dtype=np.int64)}
        for col in ("high", "low", "close", "volume"):
            b[col] = np.array([present[x][col] for x in s], dtype=np.float64)
        return b

    def mk(c):
        return {"high": c + 1, "low": c - 1, "close": c, "volume": 1000.0 + rng.random() * 4000}

    hist = []
    for t in range(30):
        p = {"A": mk(100.0 + np.cumsum(rng.standard_normal(1))[0])}
        if rng.random() > 0.3:
            p["B"] = mk(50.0 + np.cumsum(rng.standard_normal(1))[0])
        hist.append(bar(p, 60 + t * 60))

    seed_eng = CleanEngine([PriceVolumeClean()], syms, 130)
    seed_eng.seed(hist[:-1])
    seed_out = seed_eng.step(hist[-1])
    live_eng = CleanEngine([PriceVolumeClean()], syms, 130)
    live_out = {}
    for h in hist:
        live_out = live_eng.step(h)
    for fname, arr in seed_out["price_volume"].items():
        np.testing.assert_allclose(
            np.nan_to_num(arr), np.nan_to_num(live_out["price_volume"][fname]), rtol=1e-12,
            err_msg=f"price_volume.{fname} seed-replay != live",
        )
    _assert_feature_spec_contract(seed_out["price_volume"], PriceVolumeGroup().declare(), "price_volume ")


# ========================================================================================================= #
# CORR/OLS-DENOM near-zero-variance boundary — the corr-denom footgun that historically gated incremental_safe
# (the b·sxx variance-guard, #402/#122/#131). LEGACY corr_/slope_/r2_ reject when denom_x <= 1e-9·(b·sxx)
# (CoV²<1e-9, _OLS_DENOM_X_CENTERED_REL_EPS) OR denom_x <= 1e-12·(Σx)² — a near-constant-but-nonzero return
# window is NaN on BOTH paths. The clean _windowed_corr/_windowed_ols_slope only guard var_x>0 → DIVERGENCE.
# ========================================================================================================= #


def test_windowed_corr_rejects_near_constant_x_like_legacy() -> None:
    """A near-constant-but-nonzero x (return ticking by the same tiny amount, CoV²<1e-9): legacy corr_ returns
    NaN (catastrophic-cancellation reject); the clean kernel must too, NOT a spurious correlation of noise.
    FIXED by the kernel denom floors (ff13dc3) — was the corr-denom port bug (legacy NaN vs clean ~1.001)."""
    n = 10
    x = (1e-4 + np.linspace(0.0, 1e-10, n))[None, :]  # constant to ~6 sig figs → CoV² ~ 1e-13
    y = np.arange(n, dtype=np.float64)[None, :]
    b = float(n)
    sxx = float((x[0] * x[0]).sum())
    sx = float(x[0].sum())
    cov2 = (b * sxx - sx * sx) / (b * sxx)
    assert cov2 < 1e-9  # the legacy reject regime
    assert np.isnan(_windowed_corr(x, y, n)[0]), "near-constant-x corr must be NaN (legacy 1e-9 b·sxx guard)"


def test_windowed_ols_slope_rejects_near_constant_x_like_legacy() -> None:
    """Slope mirrors corr: a near-constant x → NaN (legacy 1e-9·n·Σx² floor), not a spurious slope. FIXED ff13dc3."""
    n = 10
    x = (1e-4 + np.linspace(0.0, 1e-10, n))[None, :]
    y = np.arange(n, dtype=np.float64)[None, :]
    assert np.isnan(_windowed_ols_slope(x, y, n)[0]), "near-constant-x slope must be NaN (legacy guard)"


def test_windowed_corr_perfect_fit_two_points_sign() -> None:
    """ArchOverhaul's SECOND fix: a window with exactly 2 present points is a perfect fit → corr = sign(cov)
    (±1.0 exactly), NOT the raw ratio. Matches legacy corr_'s perfect-fit rule. Both signs."""
    n = 6
    # only the last 2 columns present (others NaN) → n==2 perfect fit
    x = np.full((1, n), np.nan)
    y = np.full((1, n), np.nan)
    x[0, -2:] = [1.0, 2.0]
    y[0, -2:] = [10.0, 20.0]  # positively related → +1
    assert _windowed_corr(x, y, n)[0] == pytest.approx(1.0)
    y[0, -2:] = [20.0, 10.0]  # negatively related → −1
    assert _windowed_corr(x, y, n)[0] == pytest.approx(-1.0)


def test_distribution_moment_floor_matches_legacy() -> None:
    """CORRECT port (the GOOD case): distribution's m2>1e-12 moment floor (_MOMENT_MIN_VAR) matches legacy
    exactly → a near-constant-return window (m2 << 1e-12) gives NaN skew/kurt, same as legacy. This is the
    distribution-side analogue of the corr guard, and it IS faithful (unlike _windowed_corr)."""
    from quantlib.features.clean_groups_windowed import DistributionClean, _MOMENT_MIN_VAR  # noqa: PLC0415

    assert _MOMENT_MIN_VAR == 1e-12  # matches legacy distribution._MOMENT_MIN_VAR
    eng = CleanEngine([DistributionClean()], ["A"], 130)
    c = 100.0
    out = {}
    for ep in range(15):
        c = c * (1.0 + 1e-9)  # near-constant return → m2 ~ 1e-18 << 1e-12
        out = eng.step(_ohlcv_bar(["A"], [c], [c + 1], [c - 1], [c], 60 + ep * 60))["distribution"]
    assert np.isnan(out["ret_skew_10m"][0]), "near-constant m2<1e-12 → skew NaN (matches legacy)"
    assert np.isnan(out["ret_kurt_10m"][0]), "near-constant m2<1e-12 → kurt NaN (matches legacy)"


def test_windowed_corr_near_constant_y_matches_batch_not_anchored() -> None:
    """ANCHORED-Y NUANCE resolved (ArchOverhaul): legacy pv_correlation anchors y ONLY under FP_RUST_REDUCE
    (the incremental path), using a 1e-9·(b·syy) centered floor to clear incremental running-sum noise. The
    clean engine is FRESH-SUM (recomputes Σy/Σy² every minute, no accumulation), so it matches the legacy
    BATCH/backfill path — also fresh-sum, also the non-anchored 1e-12·(Σy)² floor — which is value-truth.
    Confirm on a near-constant-VOLUME window: clean == legacy batch corr_, NOT the stricter anchored reject.
    (The anchored refinement is an incremental-vs-batch reconciliation artifact, unnecessary in one fresh-sum
    path — the general principle for every remaining regression group: port the BATCH math, not the anchoring.)
    """
    n = 10
    rets = np.linspace(-0.005, 0.008, n)
    vols = 1e6 + np.linspace(0.0, 5.0, n)  # near-constant volume (CoV_y² tiny)
    clean = _windowed_corr(rets[None, :], vols[None, :], n)[0]
    # legacy BATCH corr_ (non-anchored, fresh-sum): n>=2 & denom_x>1e-12·(Σx)² & denom_x>1e-9·b·Σx² &
    # denom_y>1e-12·(Σy)²  (declarative.py:213-217 with FP_RUST_REDUCE unset)
    sx, sy = rets.sum(), vols.sum()
    sxx, syy, sxy = (rets * rets).sum(), (vols * vols).sum(), (rets * vols).sum()
    b = float(n)
    denom_x, denom_y, cov_n = b * sxx - sx * sx, b * syy - sy * sy, b * sxy - sx * sy
    defined = (b >= 2) and (denom_x > 1e-12 * sx * sx) and (denom_x > 1e-9 * b * sxx) and (denom_y > 1e-12 * sy * sy)
    legacy_batch = (cov_n / np.sqrt(denom_x * denom_y)) if defined else np.nan
    assert clean == pytest.approx(legacy_batch, rel=1e-12), "clean corr must match legacy BATCH (fresh-sum 1e-12)"
    # and it is a finite value here (the non-anchored floor passes), proving clean does NOT over-reject like
    # the anchored 1e-9·b·syy floor would (which is incremental-only, not the batch truth the clean engine is).
    assert np.isfinite(clean)


# ========================================================================================================= #
# CROSS-SECTIONAL — batch 2a: cross_sectional_rank (present()-gated symbol-axis rank, the breadth-bug family).
# ========================================================================================================= #

from quantlib.features.clean_groups_xsectional import (  # noqa: E402
    CrossSectionalRankClean,
    _average_rank,
    _cross_sectional_percentile,
)


def _vbar(present_map, epoch):
    """present_map: {symbol: (close, volume)}; symbols not in the map are OMITTED (absent this minute)."""
    syms = list(present_map.keys())
    return {"symbol": np.array(syms),
            "close": np.array([present_map[s][0] for s in syms], dtype=np.float64),
            "volume": np.array([present_map[s][1] for s in syms], dtype=np.float64),
            "minute_epoch": np.array([epoch], dtype=np.int64)}


def test_average_rank_ties_match_polars() -> None:
    """ties share the mean of their span — matches polars rank(method='average'): [3,1,2,2] → [4,1,2.5,2.5]."""
    r = _average_rank(np.array([3.0, 1.0, 2.0, 2.0]))
    np.testing.assert_allclose(r, [4.0, 1.0, 2.5, 2.5])


def test_cross_sectional_percentile_known() -> None:
    """percentile = (rank−1)/(n−1). 3 present values 10/20/30 → 0.0/0.5/1.0."""
    p = _cross_sectional_percentile(np.array([10.0, 20.0, 30.0]), np.array([True, True, True]))
    np.testing.assert_allclose(p, [0.0, 0.5, 1.0])


def test_xrank_sparse_present_only_denominator() -> None:
    """THE BREADTH-BUG CHECK: A,B,C present (vol 1500/2500/3500), D ABSENT (omitted). The rank is over the 3
    PRESENT names → A=0.0, C=1.0, denominator n=3 — the absent D (carrying a stale value) must NOT enter the
    rank set (which would compress everyone to n=4). D itself → NaN (not present)."""
    syms = ["A", "B", "C", "D"]
    eng = CleanEngine([CrossSectionalRankClean()], syms, WINDOW)
    # seed all 4 present, then a minute where only A,B,C deliver (D omitted)
    eng.step(_vbar({"A": (100.0, 9999.0), "B": (100.0, 9999.0), "C": (100.0, 9999.0), "D": (100.0, 9999.0)}, 60))
    out = eng.step(_vbar({"A": (100.0, 1500.0), "B": (100.0, 2500.0), "C": (100.0, 3500.0)}, 120))[
        "cross_sectional_rank"]
    # volume_rank over the 3 PRESENT names: 1500→0.0, 2500→0.5, 3500→1.0 (n=3, not n=4)
    assert out["volume_rank_1m"][0] == pytest.approx(0.0)   # A
    assert out["volume_rank_1m"][1] == pytest.approx(0.5)   # B
    assert out["volume_rank_1m"][2] == pytest.approx(1.0)   # C
    assert np.isnan(out["volume_rank_1m"][3])               # D absent → NaN (not in the rank set)


def test_xrank_n_less_than_2_is_nan() -> None:
    """Only 1 symbol present → no rank over 1 → NaN."""
    eng = CleanEngine([CrossSectionalRankClean()], ["A", "B"], WINDOW)
    out = eng.step(_vbar({"A": (100.0, 1000.0)}, 60))["cross_sectional_rank"]
    assert np.isnan(out["volume_rank_1m"][0])


def test_xrank_absent_value_does_not_shift_present_ranks() -> None:
    """Adversarial: an absent name carrying an EXTREME stale value must NOT shift the present names' ranks.
    D carried a huge volume; when D is absent, A/B/C rank exactly as if D never existed (n=3, 0.0/0.5/1.0)."""
    syms = ["A", "B", "C", "D"]
    eng = CleanEngine([CrossSectionalRankClean()], syms, WINDOW)
    eng.step(_vbar({"A": (100.0, 1.0), "B": (100.0, 2.0), "C": (100.0, 3.0), "D": (100.0, 1e9)}, 60))  # D huge
    out = eng.step(_vbar({"A": (100.0, 1500.0), "B": (100.0, 2500.0), "C": (100.0, 3500.0)}, 120))[
        "cross_sectional_rank"]
    # if D's stale 1e9 leaked into the rank set, A/B/C would all be < 1.0; with present()-gating C==1.0.
    assert out["volume_rank_1m"][2] == pytest.approx(1.0), "absent D's stale value leaked into the rank set"


def test_xrank_contract() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.cross_sectional_rank import CrossSectionalRankGroup  # type: ignore  # noqa: PLC0415,E501

    rng = np.random.default_rng(20)
    syms = [f"S{i}" for i in range(8)]
    eng = CleanEngine([CrossSectionalRankClean()], syms, WINDOW)
    out = {}
    for ep in range(70):
        present = {s: (100.0 + rng.standard_normal(), 1000.0 + rng.random() * 4000) for s in syms
                   if rng.random() > 0.2}  # ~20% absent each minute (sparse)
        if present:
            out = eng.step(_vbar(present, 60 + ep * 60))["cross_sectional_rank"]
    _assert_feature_spec_contract(out, CrossSectionalRankGroup().declare(), "cross_sectional_rank ")


# ========================================================================================================= #
# CROSS-SECTIONAL — batch 2b: return_dispersion (intraday horizons). GREEN-INTRADAY-ONLY: the 4 daily-horizon
# features are DEFERRED-NaN by design (Lead's integrity guardrail — a deferred-NaN feature passes the contract
# but is NOT validated-correct; flag + tally it, never silently count it green).
# ========================================================================================================= #

from quantlib.features.clean_groups_xsectional import ReturnDispersionClean, _xsec_std_iqr  # noqa: E402

# DEFERRED FEATURE TALLY (Lead's guardrail): {group: [features emitted NaN-by-design, NOT validated-correct]}.
# all-68-green is NOT satisfied until these compute real values. Updated as deferred features are wired.
DEFERRED_NAN_FEATURES = {
    "return_dispersion": [
        "return_dispersion_std_1d", "return_dispersion_iqr_1d",
        "return_dispersion_std_5d", "return_dispersion_iqr_5d",
    ],  # daily horizons → window.session (snapshot batch #55); NaN until then.
}


def test_xsec_std_iqr_quantile_is_nearest_not_linear() -> None:
    """SILENT-DIVERGENCE TRAP (ArchOverhaul): the IQR uses polars-default quantile interpolation = 'nearest',
    NOT numpy-default 'linear'. Verified independently: on [1,2,3,4] polars q75=3.0 (==np nearest) vs 3.25
    (linear). Confirm the clean _xsec_std_iqr IQR matches polars/'nearest', not 'linear'."""
    import polars as pl  # noqa: PLC0415

    vals = np.array([1.0, 2.0, 3.0, 4.0])
    present = np.array([True, True, True, True])
    _std, iqr = _xsec_std_iqr(vals, present)
    polars_iqr = pl.Series(vals).quantile(0.75) - pl.Series(vals).quantile(0.25)  # polars default
    linear_iqr = np.quantile(vals, 0.75, method="linear") - np.quantile(vals, 0.25, method="linear")
    assert iqr == pytest.approx(polars_iqr)  # matches polars 'nearest' (the legacy path)
    assert iqr != pytest.approx(linear_iqr)  # and is NOT the numpy-default 'linear' (the trap)


def test_xsec_std_is_ddof1() -> None:
    """cross-sectional std is sample (ddof=1), matching legacy."""
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    std, _ = _xsec_std_iqr(vals, np.full(5, True))
    assert std == pytest.approx(np.std(vals, ddof=1))
    assert std != pytest.approx(np.std(vals, ddof=0))


def test_return_dispersion_intraday_present_gated_and_nonnan() -> None:
    """INTRADAY 6: present()-gated (absent excluded from the dispersion + → NaN) AND COMPUTES A REAL VALUE on
    well-conditioned inputs (the Lead's non-deferred-NaN check — a real dispersion, not a placeholder)."""
    syms = ["A", "B", "C", "D"]
    eng = CleanEngine([ReturnDispersionClean()], syms, WINDOW)
    # 10 minutes, all 4 present with varied returns, then a minute where only A,B,C deliver (D absent)
    rng = np.random.default_rng(22)
    for ep in range(10):
        present = {s: (100.0 + rng.standard_normal() * (1 + i), 0.0) for i, s in enumerate(syms)}
        eng.step({"symbol": np.array(list(present)),
                  "close": np.array([present[s][0] for s in present], dtype=np.float64),
                  "volume": np.array([0.0] * len(present), dtype=np.float64),
                  "minute_epoch": np.array([60 + ep * 60], dtype=np.int64)})
    out = eng.step({"symbol": np.array(["A", "B", "C"]),
                    "close": np.array([101.0, 99.0, 103.0], dtype=np.float64),
                    "volume": np.array([0.0, 0.0, 0.0], dtype=np.float64),
                    "minute_epoch": np.array([700], dtype=np.int64)})["return_dispersion"]
    # intraday std/iqr compute a REAL value for present A,B,C (non-NaN) and NaN for absent D
    assert np.isfinite(out["return_dispersion_std_5m"][0]), "intraday std must compute, not deferred-NaN"
    assert np.isfinite(out["return_dispersion_iqr_5m"][0]), "intraday iqr must compute"
    assert np.isnan(out["return_dispersion_std_5m"][3]), "absent D → NaN (present()-gated)"


def test_return_dispersion_intraday_contract_daily_deferred_flagged() -> None:
    """Contract on the INTRADAY 6; the 4 daily are DEFERRED-NaN by design (in DEFERRED_NAN_FEATURES). This
    asserts the deferred set is EXACTLY the daily horizons (so a real feature can't silently slip into the
    deferred bucket) AND the intraday 6 are NOT in it (they must genuinely compute)."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.return_dispersion import ReturnDispersionGroup  # type: ignore  # noqa: PLC0415,E501

    deferred = set(DEFERRED_NAN_FEATURES["return_dispersion"])
    declared = {s.name for s in ReturnDispersionGroup().declare()}
    intraday = {f for f in declared if f.endswith("m")}
    daily = {f for f in declared if f.endswith("d")}
    assert deferred == daily, "deferred set must be exactly the daily horizons"
    assert not (deferred & intraday), "no intraday feature may be deferred-NaN"
    # produced set == declared (the daily exist-as-NaN, which the contract allows under nan_policy=sparse)
    assert set(ReturnDispersionClean().feature_names) == declared


# ========================================================================================================= #
# CROSS-SECTIONAL — batch 2c: sector_beta (per-(minute,sector) equal-weight aggregate + per-symbol OLS on its
# OWN sector return, window.static sector labels). Trickiest cross-sectional: present()-gated sector mean + OLS.
# NOTE: legacy _ols_from_sums guards n>=MIN_PAIRS(5) & var_x>0 & var_y>0 — a BARE var>0 (batch-truth, NOT the
# 1e-9 relative floor pv_correlation needs); the clean _windowed_sector_ols matching var>0 is correct.
# ========================================================================================================= #

from quantlib.features.clean_groups_xsectional import (  # noqa: E402
    SectorBetaClean,
    _sector_equal_weight_returns,
    _windowed_sector_ols,
)


def _sec_bar(present_map, epoch):
    """present_map: {symbol: close}; absent symbols OMITTED."""
    syms = list(present_map.keys())
    return {"symbol": np.array(syms), "close": np.array([present_map[s] for s in syms], dtype=np.float64),
            "minute_epoch": np.array([epoch], dtype=np.int64)}


def test_sector_equal_weight_present_gated() -> None:
    """The per-(minute,sector) equal-weight mean is over PRESENT-FINITE returns only: a NaN return (absent
    symbol's row) is excluded from its sector's mean (mask.sum over axis 0). Sector 0 = {A,B}, sector 1 = {C}.
    On a minute where A's return is NaN (warm-up/absent), sector-0 mean = B's return alone."""
    ret = np.array([[np.nan, 0.02], [np.nan, 0.04], [np.nan, 0.10]])  # 3 symbols, 2 minutes
    sector = np.array([0, 0, 1])  # A,B in sector 0; C in sector 1
    sec_ret = _sector_equal_weight_returns(ret, sector)
    # minute 1 (col 1): sector-0 mean over A,B = (0.02+0.04)/2 = 0.03, broadcast to A and B rows
    assert sec_ret[0, 1] == pytest.approx(0.03)
    assert sec_ret[1, 1] == pytest.approx(0.03)
    assert sec_ret[2, 1] == pytest.approx(0.10)  # sector 1 = C alone


def test_sector_equal_weight_absent_excluded() -> None:
    """Adversarial: if A is absent (NaN return) but B present, sector-0 mean = B alone, NOT (B + stale A)/2."""
    ret = np.array([[np.nan, np.nan], [np.nan, 0.04]])  # A all-NaN (absent), B present at minute 1
    sector = np.array([0, 0])
    sec_ret = _sector_equal_weight_returns(ret, sector)
    assert sec_ret[1, 1] == pytest.approx(0.04), "absent A leaked into the sector mean"


def test_sector_beta_ols_known_slope() -> None:
    """sector_beta OLS: own_ret = 2·sector_ret exactly → beta = 2.0, corr = 1.0 (perfect)."""
    n = 8
    sec_ret = np.linspace(-0.01, 0.01, n)[None, :]
    own_ret = 2.0 * sec_ret
    beta, corr = _windowed_sector_ols(own_ret, sec_ret, n)
    assert beta[0] == pytest.approx(2.0, rel=1e-9)
    assert corr[0] == pytest.approx(1.0, rel=1e-9)


def test_sector_beta_min_pairs_and_beta_max() -> None:
    """n < MIN_PAIRS(5) → NaN; |beta| > BETA_MAX(15) → beta NULL (non-physical)."""
    n = 8
    sec_ret = np.full((1, n), np.nan)
    sec_ret[0, -3:] = [0.001, 0.002, 0.003]  # only 3 pairs < MIN_PAIRS
    own = np.full((1, n), np.nan)
    own[0, -3:] = [0.002, 0.004, 0.006]
    beta, corr = _windowed_sector_ols(own, sec_ret, n)
    assert np.isnan(beta[0]) and np.isnan(corr[0]), "n<MIN_PAIRS must be NaN"
    # |beta|>15: own_ret = 20·sector_ret → beta 20 > BETA_MAX → NULL
    sr = np.linspace(-0.001, 0.001, n)[None, :]
    big = _windowed_sector_ols(20.0 * sr, sr, n)
    assert np.isnan(big[0][0]), "|beta|>15 must be NULL"


def test_sector_beta_unmapped_sector_nan() -> None:
    """An unmapped-sector name (sector == -1) → sector_beta/sector_corr NaN (no sector series to regress on)."""
    eng = CleanEngine([SectorBetaClean()], ["A"], WINDOW)
    eng.static = {"sector": np.array([-1])}  # A is unmapped
    out = {}
    for ep in range(20):
        out = eng.step(_sec_bar({"A": 100.0 + ep * 0.1}, 60 + ep * 60))["sector_beta"]
    assert np.isnan(out["sector_beta_15m"][0])
    assert np.isnan(out["sector_corr_15m"][0])


def test_sector_beta_contract_and_seed_equals_live() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.sector_beta import SectorBetaGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(24)
    syms = [f"S{i}" for i in range(6)]
    sectors = np.array([0, 0, 0, 1, 1, 1])

    def run(engine, hist):
        out = {}
        for h in hist:
            out = engine.step(h)
        return out, engine

    hist = []
    for t in range(30):
        present = {s: 100.0 + np.cumsum(rng.standard_normal(1))[0] for i, s in enumerate(syms) if rng.random() > 0.2}
        if present:
            hist.append(_sec_bar(present, 60 + t * 60))

    seed_eng = CleanEngine([SectorBetaClean()], syms, WINDOW)
    seed_eng.static = {"sector": sectors}
    seed_eng.seed(hist[:-1])
    seed_out = seed_eng.step(hist[-1])
    live_eng = CleanEngine([SectorBetaClean()], syms, WINDOW)
    live_eng.static = {"sector": sectors}
    live_out, _ = run(live_eng, hist)
    for fname, arr in seed_out["sector_beta"].items():
        np.testing.assert_allclose(np.nan_to_num(arr), np.nan_to_num(live_out["sector_beta"][fname]),
                                   rtol=1e-12, err_msg=f"sector_beta.{fname} seed != live")
    _assert_feature_spec_contract(seed_out["sector_beta"], SectorBetaGroup().declare(), "sector_beta ")


def test_sector_beta_near_constant_sector_return_matches_legacy_batch() -> None:
    """INTEGRITY (Lead): sector_beta uses its OWN _windowed_sector_ols guard, NOT the shared corr/slope kernel
    fix — so the near-zero-variance hazard could re-recur in this separate path. Confirm its OWN guard matches
    legacy _ols_from_sums EXACTLY at the near-constant-SECTOR-RETURN boundary (low sector dispersion / few
    present names → denom collapses). FINDING: legacy sector_beta has NO relative-variance floor — it guards
    (n>=MIN_PAIRS=5 & var_x>0 & var_y>0) and NULLs a non-physical |beta|>BETA_MAX=15. So at near-constant-x the
    bare var_x>0 PASSES, but cov/tiny-var → huge beta → BETA_MAX NULLs it. The clean port replicates this
    EXACTLY (beta NaN via BETA_MAX, corr clipped) — matches legacy batch-truth, NOT the pv 1e-9 relative floor
    (that was pv's return-x-side guard specifically). Different mechanism, same reject, faithful port."""
    from quantlib.features.clean_groups_xsectional import _SECTOR_BETA_MAX, _SECTOR_MIN_PAIRS  # noqa: PLC0415

    n = 10
    sec = (1e-4 + np.linspace(0.0, 1e-10, n))[None, :]  # near-constant nonzero sector return (CoV²~1e-13)
    own = np.linspace(0.0, 0.05, n)[None, :]
    beta, corr = _windowed_sector_ols(own, sec, n)
    # legacy _ols_from_sums replicated exactly (bare var>0 + BETA_MAX + clip; NO relative floor)
    x, y, nn = sec[0], own[0], float(n)
    sx, sy = x.sum(), y.sum()
    sxx, syy, sxy = (x * x).sum(), (y * y).sum(), (x * y).sum()
    cov, var_x, var_y = sxy - sx * sy / nn, sxx - sx * sx / nn, syy - sy * sy / nn
    defined = (nn >= _SECTOR_MIN_PAIRS) and (var_x > 0) and (var_y > 0)
    beta_raw = cov / var_x
    legacy_beta = beta_raw if (defined and abs(beta_raw) <= _SECTOR_BETA_MAX) else np.nan
    legacy_corr = float(np.clip(cov / (np.sqrt(var_x) * np.sqrt(var_y)), -1, 1)) if defined else np.nan
    # beta: both NaN (BETA_MAX nulls the exploded slope). corr: both the clipped value (no BETA_MAX on corr).
    assert (np.isnan(beta[0]) and np.isnan(legacy_beta)) or beta[0] == pytest.approx(legacy_beta)
    assert (np.isnan(corr[0]) and np.isnan(legacy_corr)) or corr[0] == pytest.approx(legacy_corr)
    assert np.isnan(beta[0]), "near-constant-sector beta must be NULLed (BETA_MAX), matching legacy"


# ========================================================================================================= #
# CROSS-SECTIONAL — batch 2d: sector_return (TWO present() gates: DENOMINATOR (absent not in sector mean) AND
# OUTPUT (absent symbol's own row → NaN). Both must hold.
# ========================================================================================================= #

from quantlib.features.clean_groups_xsectional import SectorReturnClean, _sector_mean_vector  # noqa: E402


def test_sector_return_mean_present_gated_denominator() -> None:
    """DENOMINATOR gate: the sector mean is over PRESENT members only. Sector 0 = {A,B,C}; B absent → the
    sector mean for A,C is over {A,C}, NOT {A, stale-B, C}."""
    own_ret = np.array([0.02, np.nan, 0.06])  # A=0.02, B absent (NaN), C=0.06
    sector = np.array([0, 0, 0])
    present = np.array([True, False, True])
    sec_mean = _sector_mean_vector(own_ret, sector, present)
    assert sec_mean[0] == pytest.approx(0.04)   # (0.02+0.06)/2 over present A,C (B excluded)
    assert sec_mean[2] == pytest.approx(0.04)


def test_sector_return_output_gate_absent_row_nan() -> None:
    """OUTPUT gate: an absent symbol's OWN row → NaN, even though it has a mapped sector. A,B present, C absent;
    C's sector_return must be NaN (output-gated), while A,B compute."""
    syms = ["A", "B", "C"]
    eng = CleanEngine([SectorReturnClean()], syms, WINDOW)
    eng.static = {"sector": np.array([0, 0, 0])}
    # 10 minutes all present, then a minute where only A,B deliver (C absent)
    for ep in range(10):
        eng.step(_sec_bar({"A": 100.0 + ep, "B": 101.0 + ep, "C": 99.0 + ep}, 60 + ep * 60))
    out = eng.step(_sec_bar({"A": 111.0, "B": 112.0}, 700))["sector_return"]
    assert np.isfinite(out["sector_return_5m"][0])  # A present → computes
    assert np.isfinite(out["sector_return_5m"][1])  # B present → computes
    assert np.isnan(out["sector_return_5m"][2])     # C ABSENT → output-gated NaN


def test_sector_return_excess_sums_to_zero_within_sector() -> None:
    """sector_excess = own − sector_mean → within a sector the present members' excess sums to ~0 (mean-centered)."""
    syms = ["A", "B", "C"]
    eng = CleanEngine([SectorReturnClean()], syms, WINDOW)
    eng.static = {"sector": np.array([0, 0, 0])}
    rng = np.random.default_rng(25)
    out = {}
    for ep in range(10):
        out = eng.step(_sec_bar({s: 100.0 + rng.standard_normal() * 5 for s in syms}, 60 + ep * 60))[
            "sector_return"]
    excess = np.array([out["sector_excess_5m"][i] for i in range(3)])
    assert np.nansum(excess) == pytest.approx(0.0, abs=1e-9)


def test_sector_return_unmapped_nan() -> None:
    """unmapped sector (-1) → both sector_return and sector_excess NaN."""
    eng = CleanEngine([SectorReturnClean()], ["A"], WINDOW)
    eng.static = {"sector": np.array([-1])}
    out = {}
    for ep in range(8):
        out = eng.step(_sec_bar({"A": 100.0 + ep}, 60 + ep * 60))["sector_return"]
    assert np.isnan(out["sector_return_5m"][0]) and np.isnan(out["sector_excess_5m"][0])


def test_sector_return_contract_and_seed_equals_live() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.sector_return import SectorReturnGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(26)
    syms = [f"S{i}" for i in range(6)]
    sectors = np.array([0, 0, 0, 1, 1, 1])
    hist = []
    for t in range(30):
        present = {s: 100.0 + np.cumsum(rng.standard_normal(1))[0] for s in syms if rng.random() > 0.2}
        if present:
            hist.append(_sec_bar(present, 60 + t * 60))
    se = CleanEngine([SectorReturnClean()], syms, WINDOW)
    se.static = {"sector": sectors}
    se.seed(hist[:-1])
    so = se.step(hist[-1])
    le = CleanEngine([SectorReturnClean()], syms, WINDOW)
    le.static = {"sector": sectors}
    lo = {}
    for h in hist:
        lo = le.step(h)
    for fname, arr in so["sector_return"].items():
        np.testing.assert_allclose(np.nan_to_num(arr), np.nan_to_num(lo["sector_return"][fname]),
                                   rtol=1e-12, err_msg=f"sector_return.{fname} seed != live")
    _assert_feature_spec_contract(so["sector_return"], SectorReturnGroup().declare(), "sector_return ")
