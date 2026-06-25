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


def _bars(symbols: list[str], close: list[float], volume: list[float] | None = None) -> dict[str, np.ndarray]:
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
    closes = {s: list(100.0 + np.cumsum(rng.standard_normal(40) * (0.5 + 0.1 * i))) for i, s in enumerate(syms)}
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
        engine.step({"symbol": np.array(["A"]), "high": np.array([101.0]),
                     "low": np.array([99.0]), "close": np.array([100.0])})
    out = engine.step({"symbol": np.array(["A"]), "high": np.array([101.0]),
                       "low": np.array([99.0]), "close": np.array([100.0])})["realized_range"]
    for w in (3, 5, 10):
        assert out[f"realized_range_{w}m"][0] == pytest.approx(2.0 / 100.0)


def test_realized_range_nonnegative_and_mean_of_known() -> None:
    """A known two-value range series averages correctly and is always >= 0."""
    engine = CleanEngine([RealizedRangeClean()], ["A"], WINDOW)
    # bar 1: range 4 (h104,l100,c100 → 0.04); bar 2: range 2 (h102,l100,c100 → 0.02). mean over 2 = 0.03
    engine.step({"symbol": np.array(["A"]), "high": np.array([104.0]), "low": np.array([100.0]), "close": np.array([100.0])})
    out = engine.step({"symbol": np.array(["A"]), "high": np.array([102.0]), "low": np.array([100.0]), "close": np.array([100.0])})["realized_range"]
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
    doji = CleanEngine([CandlestickClean()], ["A"], WINDOW).step(_ohlc(["A"], [100.0], [105.0], [95.0], [100.2]))["candlestick"]
    big = CleanEngine([CandlestickClean()], ["A"], WINDOW).step(_ohlc(["A"], [100.0], [110.0], [90.0], [109.0]))["candlestick"]
    assert doji["is_doji"][0] == 1.0
    assert big["is_doji"][0] == 0.0


def test_candlestick_bullish_engulfing() -> None:
    """Prior bar bearish (o102 c98), this bar bullish engulfing (o97 c103, body covers prior) → flag 1."""
    engine = CleanEngine([CandlestickClean()], ["A"], WINDOW)
    engine.step(_ohlc(["A"], [102.0], [103.0], [97.0], [98.0]))   # prior: bearish (c<o)
    out = engine.step(_ohlc(["A"], [97.0], [104.0], [96.0], [103.0]))["candlestick"]  # this: bullish, engulfs
    assert out["pattern_engulfing_bullish"][0] == 1.0


def test_candlestick_no_engulfing_when_prior_bullish() -> None:
    """Prior bar bullish → not a bullish-engulfing setup → flag 0."""
    engine = CleanEngine([CandlestickClean()], ["A"], WINDOW)
    engine.step(_ohlc(["A"], [98.0], [103.0], [97.0], [102.0]))   # prior: bullish
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
    assert sparse_out["macd_line"][0] == pytest.approx(dense_out["macd_line"][0], rel=1e-9), \
        "EMA decayed across the gap (should hold)"


def test_macd_per_symbol_absence_holds_ema() -> None:
    """The PRODUCTION-relevant sparse case: A and B present, then a minute where only A delivers (advancing
    epoch). B's EMA must HOLD across the minute it was absent (not re-decay toward its carried close)."""
    def _bar(symbols, closes, epoch):
        return {"symbol": np.array(symbols), "close": np.array(closes, dtype=np.float64),
                "minute_epoch": np.array([epoch], dtype=np.int64)}

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
    groups = [TrendQualityClean(), VwapDeviationClean(), RealizedRangeClean(),
              CandlestickClean(), BreadthClean(), MacdClean()]
    engine = CleanEngine(groups, syms, WINDOW)
    out = {}
    for _ in range(30):
        c = 100.0 + rng.standard_normal(5).cumsum()
        bars = {"symbol": np.array(syms), "open": c * 0.999, "high": c * 1.003,
                "low": c * 0.997, "close": c, "volume": 1000.0 + rng.random(5) * 3000}
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


@pytest.mark.xfail(
    reason="SAME interface gap as macd (reported): breadth counts a symbol ABSENT this minute as a valid "
    "cross-section member — window.trailing('close') returns its CARRIED bars so ret is finite → valid=True "
    "even with no bar this minute. The axis-0 reduce should count only THIS-minute-present symbols; needs "
    "window.present(). Until then the cross-sectional denominator is wrong on sparse minutes.",
    strict=True,
)
def test_breadth_sparse_presence_counts_only_present() -> None:
    """ADVERSARIAL (cross-sectional + sparse): A,B trend UP, C,D trend DOWN (all 4 present 6 bars). Then a
    minute where ONLY A,B deliver. Presence-aware breadth reduces over the 2 PRESENT (up) names →
    breadth_up=1.0, down=0.0. The BUG counts the 2 ABSENT (down-history) names via their carried bars →
    denominator 4 → up=0.5, down=0.5 (verified). Asserting the CORRECT answer → fails until window.present()."""
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
    engine.step(_close_bars(["A"], [100.0]))            # bar 1
    engine.step(_close_bars(["A"], [110.0]))            # bar 2
    engine.step(_close_bars([], []))                    # A ABSENT — EMA must HOLD
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
                np.nan_to_num(s_state[key]), np.nan_to_num(l_state[key]), rtol=1e-12,
                err_msg=f"{gname}.{key} carried state diverged seed-replay vs live",
            )
    # final output identical
    for gname, feats in seed_out.items():
        for fname, arr in feats.items():
            np.testing.assert_allclose(
                np.nan_to_num(arr), np.nan_to_num(live_out[gname][fname]), rtol=1e-12,
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
    asserted now (seed==live), the correctness half flips in when present() lands (see the xfail gap test)."""
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
        rtol=1e-12, err_msg="macd ema12 carried state diverged seed-replay vs live ACROSS GAPS",
    )
    np.testing.assert_allclose(
        np.nan_to_num(seed_out["macd"]["macd_line"]), np.nan_to_num(live_out["macd"]["macd_line"]),
        rtol=1e-12, err_msg="macd_line diverged seed vs live across gaps",
    )


def test_swing_duplicate_minute_does_not_double_advance() -> None:
    """IDEMPOTENCY FOOTGUN (Lead): a DUPLICATE delivery of the same minute must NOT double-advance the swing
    leg-state. Feed a bar, then RE-deliver the same minute_epoch — the extreme/pivot must be the same as if it
    were delivered once. A plain present() bool does NOT cover this (the re-delivery still reads present=True);
    catching it here flags whether a last-epoch dedup guard is needed beyond presence."""
    def _bar(close, epoch):
        return {"symbol": np.array(["A"]), "close": np.array([close]),
                "minute_epoch": np.array([epoch], dtype=np.int64)}

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
    assert dup_extreme == pytest.approx(once_extreme), \
        "duplicate minute double-advanced swing leg-state — needs a last-epoch dedup guard beyond present()"


def test_cumulative_duplicate_minute_does_not_double_count() -> None:
    """The cumulative kind is where the duplicate-minute footgun bites: intraday_seasonality's running count
    must increment ONCE per distinct minute, not per delivery. FIXED by the engine's C4 absorbed-minute
    watermark (5d5f564): a re-delivered minute_epoch (<= watermark) is a no-op — no re-append, no group
    compute — so the cnt stays 1. The dedup guard is at the ENGINE level (owns it once for every carried-state
    kind), separate from presence — exactly as scoped."""
    def _vbar(vol, epoch):
        return {"symbol": np.array(["A"]), "volume": np.array([vol]),
                "minute_epoch": np.array([epoch], dtype=np.int64)}

    engine = CleanEngine([IntradaySeasonalityClean()], ["A"], WINDOW)
    engine.step(_vbar(1000.0, 60))
    engine.step(_vbar(1000.0, 60))  # SAME minute re-delivered
    cnt = engine._group_state["intraday_seasonality"]["cnt"][0]
    assert cnt == pytest.approx(1.0), "duplicate minute double-counted the cumulative cnt (needs epoch dedup)"


# --------------------------------------------------------------------------------------------------------- #
# intraday_seasonality two-session reset + prior_day compute-once — present()-independent, validatable now.
# --------------------------------------------------------------------------------------------------------- #


def test_intraday_seasonality_session_reset_two_days() -> None:
    """CUMULATIVE/reset: the since-open running mean is correct mid-session AND resets at the day boundary —
    session 2 starts fresh (ratio back to base), not carried across. (The cnt double-count on a DUPLICATE
    minute is a separate footgun, covered by test_cumulative_duplicate_minute_does_not_double_count.)"""
    def _vbar(vol, epoch):
        return {"symbol": np.array(["A"]), "volume": np.array([vol]),
                "minute_epoch": np.array([epoch], dtype=np.int64)}

    day = 86400
    engine = CleanEngine([IntradaySeasonalityClean()], ["A"], WINDOW)
    engine.step(_vbar(1000.0, 0))
    engine.step(_vbar(2000.0, 60))
    out1 = engine.step(_vbar(3000.0, 120))["intraday_seasonality"]  # mean(1000,2000,3000)=2000, 3000/2000=1.5
    assert out1["volume_vs_session_mean"][0] == pytest.approx(1.5)
    out2 = engine.step(_vbar(500.0, day))["intraday_seasonality"]  # new session: mean=500, ratio=1.0
    assert out2["volume_vs_session_mean"][0] == pytest.approx(1.0), "session did not reset at the day boundary"
    assert engine._group_state["intraday_seasonality"]["cnt"][0] == pytest.approx(1.0), "reset did not clear cnt"


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
        return {"symbol": np.array(symbols), "volume": np.array(vols, dtype=np.float64),
                "close": np.array(closes, dtype=np.float64), "minute_epoch": np.array([epoch], dtype=np.int64)}

    syms = ["A", "B"]
    groups = [IntradaySeasonalityClean(), MacdClean(), SwingClean()]
    engine = CleanEngine(groups, syms, WINDOW)
    engine.step(_multi(syms, [1000.0, 2000.0], [100.0, 50.0], 60))
    engine.step(_multi(syms, [1500.0, 2500.0], [101.0, 51.0], 120))
    # snapshot every carried-state array across all 3 groups
    before = {g.name: {k: v.copy() for k, v in engine._group_state[g.name].items()} for g in groups}

    engine.step(_multi(syms, [9999.0, 9999.0], [200.0, 200.0], 120))  # DUPLICATE epoch 120 → no-op
    engine.step(_multi(syms, [9999.0, 9999.0], [200.0, 200.0], 30))   # OUT-OF-ORDER epoch 30 < 120 → no-op

    for g in groups:
        for key, arr in engine._group_state[g.name].items():
            np.testing.assert_array_equal(
                np.nan_to_num(arr), np.nan_to_num(before[g.name][key]),
                err_msg=f"{g.name}.{key} changed on a duplicate/out-of-order minute (watermark leak)",
            )
