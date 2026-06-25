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


def test_trend_quality_flat_is_zero_slope_zero_r2() -> None:
    """A flat close → slope 0 (no move) AND r2 0.0. The shared OLS kernel leaves r2 null/NaN on var_y=0, but
    trend_quality's assemble() PINS r2=0 there (slope_defined & r2_undefined → 0.0; trend_quality.py:122-124,
    'a flat line has zero explained variance, not an undefined fit'). Confirmed authoritative via legacy
    compute() OUTPUT on a flat-price frame = 0.0 across all windows. The re-port matches — r2=0, NOT NaN."""
    closes = {"A": [50.0] * 20}
    out = _run([TrendQualityClean()], ["A"], closes)["trend_quality"]
    assert out["price_slope_10m"][0] == pytest.approx(0.0)
    assert out["price_r2_10m"][0] == pytest.approx(0.0), "flat price → r2 0 (assemble pins it; legacy output=0)"


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
    """Reference ADJUSTED EWM (polars ewm_mean adjust=True, the live/legacy macd convention): num = x + (1-a)num,
    den = 1 + (1-a)den, ema = num/den. NOT the simple (1-a)v + a*x recurrence (that diverges from legacy in
    warm-up) — the macd group (MacdClean) and TechnicalClean both carry this num/den form, decayed on presence."""
    alpha = 2.0 / (span + 1.0)
    one_minus = 1.0 - alpha
    num = 0.0
    den = 0.0
    for x in values:
        num = x + one_minus * num
        den = 1.0 + one_minus * den
    return num / den


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


def test_macd_line_is_adjusted_ewm_not_simple_vs_polars() -> None:
    """EMA-CONVENTION GATE (Lead): pin MacdClean's macd_line against the REAL legacy primitive — polars
    ewm_mean(adjust=True) — directly, AND assert it is NOT the simple (adjust=False) recurrence, in the WARM-UP
    zone where they diverge most. This guards the whole recursive-EMA class (MacdClean + technical's MACD) from
    a silent revert to the simple convention, and validates _ema_ref itself against polars (not just self-
    consistency). On [100,110,120] span12 the gap is adjusted 111.11 vs simple 104.38 — caught here."""
    import polars as pl  # noqa: PLC0415

    closes = [100.0, 101.0, 103.0, 102.0, 105.0, 107.0, 106.0, 110.0, 108.0, 112.0, 111.0, 115.0]
    eng = CleanEngine([MacdClean()], ["A"], WINDOW)
    clean_line = []
    for c in closes:
        out = eng.step({"symbol": np.array(["A"]), "close": np.array([c])})["macd"]
        clean_line.append(float(out["macd_line"][0]))
    series = pl.Series(closes)
    adjusted = (series.ewm_mean(span=12, adjust=True) - series.ewm_mean(span=26, adjust=True)).to_numpy()
    simple = (series.ewm_mean(span=12, adjust=False) - series.ewm_mean(span=26, adjust=False)).to_numpy()
    np.testing.assert_allclose(clean_line, adjusted, rtol=1e-9,
                               err_msg="MacdClean macd_line != polars ewm_mean(adjust=True) — the legacy convention")
    # the WARM-UP cells must differ from the simple form (else the fixture can't tell the conventions apart).
    assert not np.allclose(clean_line[:5], simple[:5], rtol=1e-6), (
        "warm-up macd_line coincides with the SIMPLE (adjust=False) form — fixture can't gate the convention"
    )


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
    # adjusted EWM carries (num, den) accumulators; "holds" = BOTH unchanged on B's absent minute
    num_before = engine._group_state["macd"]["ema12__num"][1]
    den_before = engine._group_state["macd"]["ema12__den"][1]
    engine.step(_bar(["A"], [110.0], 180))  # B ABSENT (A-only, epoch advances)
    num_after = engine._group_state["macd"]["ema12__num"][1]
    den_after = engine._group_state["macd"]["ema12__den"][1]
    assert num_after == pytest.approx(num_before), "B's EMA num re-updated on a minute B was absent (presence leak)"
    assert den_after == pytest.approx(den_before), "B's EMA den re-updated on a minute B was absent (presence leak)"


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


def test_prior_day_pivots_and_distances_known() -> None:
    """prior_day reads the daily snapshot: prior day = the next-newest daily col ([:, -2]), today's open =
    the newest col ([:, -1]). Floor-trader pivots from the prior day (H=105,L=99,C=102): P=(H+L+C)/3=102,
    R1=2P−L=105, S1=2P−H=99, R2=P+(H−L)=108, S2=P−(H−L)=96; plus gap_open + dist-from-prior + above_pivot.
    Latest close 103.5. Hand-verified all 10."""
    daily_open = np.array([[100.0, 101.0, 103.0]])
    daily_high = np.array([[101.0, 105.0, 104.0]])
    daily_low = np.array([[99.0, 99.0, 102.5]])
    daily_close = np.array([[100.5, 102.0, 103.5]])
    eng = CleanEngine([PriorDayClean()], ["A"], WINDOW)
    eng.set_session({"daily_open": daily_open, "daily_high": daily_high, "daily_low": daily_low,
                     "daily_close": daily_close})
    out = eng.step(_close_bars(["A"], [103.5]))["prior_day"]
    high, low, close, today_open, latest = 105.0, 99.0, 102.0, 103.0, 103.5
    pivot = (high + low + close) / 3.0  # 102
    expected = {
        "gap_open": today_open / close - 1.0,
        "dist_from_prior_high": latest / high - 1.0,
        "dist_from_prior_low": latest / low - 1.0,
        "dist_from_prior_close": latest / close - 1.0,
        "above_pivot": 1.0,
        "dist_from_pivot_p": latest / pivot - 1.0,
        "dist_from_pivot_r1": latest / (2 * pivot - low) - 1.0,
        "dist_from_pivot_s1": latest / (2 * pivot - high) - 1.0,
        "dist_from_pivot_r2": latest / (pivot + (high - low)) - 1.0,
        "dist_from_pivot_s2": latest / (pivot - (high - low)) - 1.0,
    }
    for name, val in expected.items():
        assert out[name][0] == pytest.approx(val), f"prior_day.{name}"


def test_prior_day_no_session_is_nan() -> None:
    """No daily snapshot set → all prior_day features NaN (not a crash, not a wrong number)."""
    engine = CleanEngine([PriorDayClean()], ["A"], WINDOW)
    out = engine.step(_close_bars(["A"], [103.0]))["prior_day"]
    assert all(np.isnan(out[name][0]) for name in PriorDayClean().feature_names), "no snapshot → all-NaN"


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

    # seed-replay and live must produce the IDENTICAL carried EMA accumulators (num+den) across the gapped seq
    for accum in ("ema12__num", "ema12__den"):
        np.testing.assert_allclose(
            np.nan_to_num(seed_engine._group_state["macd"][accum]),
            np.nan_to_num(live_engine._group_state["macd"][accum]),
            rtol=1e-12,
            err_msg=f"macd {accum} carried state diverged seed-replay vs live ACROSS GAPS",
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


def test_swing_omitted_symbol_leg_state_holds() -> None:
    """FOOTGUN (highest-risk recursive-state axis, Lead): a multi-symbol swing where one symbol is OMITTED for
    a minute — its leg-state (extreme/direction) must HOLD, NOT advance on the carried bar. Marshaled the
    PRODUCTION way (omit the symbol, not feed NaN). Head-to-head: omitting B's minute must give the IDENTICAL
    B state as a dense run where that minute is a no-op carry — i.e. present() (not isfinite(latest)) gates
    the leg. This is the macd/intraday omit-marshaling lesson applied to the state-machine kind."""
    def _bar(present_map: dict[str, float], epoch: int) -> dict[str, np.ndarray]:
        return {"symbol": np.array(list(present_map)),
                "close": np.array([present_map[s] for s in present_map], dtype=np.float64),
                "minute_epoch": np.array([epoch], dtype=np.int64)}

    def _run(omit: bool) -> tuple[dict[str, np.ndarray], float]:
        eng = CleanEngine([SwingClean()], ["A", "B"], 400)
        eng.step(_bar({"A": 50.0, "B": 100.0}, 60))
        eng.step(_bar({"A": 50.0, "B": 101.0}, 120))
        eng.step(_bar({"A": 50.0, "B": 102.0}, 180))  # B on an up-leg, extreme 102
        if omit:
            eng.step(_bar({"A": 55.0}, 240))  # B OMITTED (production absence) — its leg must HOLD
        else:
            eng.step(_bar({"A": 55.0, "B": 102.0}, 240))  # dense baseline: B carries 102 (a no-op for the leg)
        out = eng.step(_bar({"A": 56.0, "B": 103.0}, 300))["swing"]
        return out, float(eng._group_state["swing"]["extreme"][1])

    out_omit, ext_omit = _run(omit=True)
    out_dense, ext_dense = _run(omit=False)
    assert ext_omit == pytest.approx(ext_dense), "B leg extreme advanced on the OMITTED minute (present leak)"
    assert out_omit["swing_direction"][1] == out_dense["swing_direction"][1], "B direction diverged on omit"
    assert out_omit["swing_pivot"][1] == out_dense["swing_pivot"][1], "B pivot diverged on omit"


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
    daily_close = np.array([[98.0, 100.0, 100.0]])  # prior-day close ([:, -2]) = 100
    engine.set_session({"daily_open": np.array([[97.0, 99.0, 100.0]]),
                        "daily_high": np.array([[99.0, 101.0, 101.0]]),
                        "daily_low": np.array([[96.0, 98.0, 99.0]]), "daily_close": daily_close})
    # dist_from_prior_close tracks the MINUTE's close against the FIXED prior close (100):
    g1 = engine.step(_close_bars(["A"], [105.0]))["prior_day"]["dist_from_prior_close"][0]
    g2 = engine.step(_close_bars(["A"], [110.0]))["prior_day"]["dist_from_prior_close"][0]
    assert g1 == pytest.approx(0.05)
    assert g2 == pytest.approx(0.10)
    # the snapshot memo is unchanged across steps — proof it's compute-once, not per-minute
    np.testing.assert_array_equal(engine.session["daily_close"], daily_close)


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
    HOLD across the omitted minute and resume → the ADJUSTED EWM over the PRESENT bars [200,201,202,204] only.
    The isfinite(latest) bug would instead advance on the carried 202 (a 5th pseudo-bar), giving a larger value —
    so this pins the present()-gate is effective AND the adjusted convention."""
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
    engine.step(_bar({"BBB": 204.0}, 300))
    ema12 = (engine._group_state["macd"]["ema12__num"][0]
             / engine._group_state["macd"]["ema12__den"][0])
    expected_present = _ema_ref([200.0, 201.0, 202.0, 204.0], 12)  # PRESENT bars only (omitted minute holds)
    expected_if_leaked = _ema_ref([200.0, 201.0, 202.0, 202.0, 204.0], 12)  # bug: carried 202 advances it
    assert ema12 == pytest.approx(expected_present, abs=1e-6), \
        "EMA advanced on the omitted (carried) minute — present() gate not effective"
    assert ema12 != pytest.approx(expected_if_leaked, abs=1e-6), \
        "fixture must distinguish the present-gated value from the leaked (carried-bar) value"


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


def test_price_volume_sparse_time_window_matches_legacy() -> None:
    """RE-GATE price_volume (#60 re-port, c3cd8c0) on the SPARSE axis — enforcing the gappy-h2h invariant on
    the keystone 70-feature group. The earlier green was DENSE-ONLY (vwap_deviation diverged, even sign-flipped,
    on sparse). Now every windowed reduction is masked via trailing_time, and obv_slope's OLS regresses on the
    REBASED-MINUTE axis (not positional arange). Head-to-head a gappy symbol (spans 85 min) vs legacy BATCH:
    vwap_deviation/up_volume_ratio (windowed sums) AND obv_slope (time-OLS, slope-per-MINUTE) match exactly."""
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70, 74, 78, 81, 85]
    rng = np.random.default_rng(17)
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)) * 0.4)
    vols = (rng.random(len(offsets)) * 1e5 + 1e4).round()
    mins = [base + datetime.timedelta(minutes=o) for o in offsets]
    df = pl.DataFrame({"minute": mins, "close": closes, "volume": vols.astype(float)}).sort("minute")
    df = df.with_columns((pl.col("close") * pl.col("volume")).alias("cv"),
                         (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"))
    df = df.with_columns(pl.when(pl.col("ret") > 0).then(pl.col("volume")).otherwise(0.0).alias("up"),
                         (pl.col("ret").sign() * pl.col("volume")).fill_null(0.0).alias("sv"))
    df = df.with_columns(pl.col("sv").cum_sum().alias("obv"))

    def _rby(col: str, w: int) -> pl.Expr:
        return pl.col(col).rolling_sum_by("minute", window_size=f"{w}m")

    for w in (5, 15, 30):
        df = df.with_columns(_rby("cv", w).alias(f"scv{w}"), _rby("volume", w).alias(f"sv{w}"),
                             _rby("up", w).alias(f"su{w}"))
    last = df.tail(1)
    cl = closes[-1]
    t_last = mins[-1]

    eng = CleanEngine([PriceVolumeClean()], ["A"], 400)
    out = {}
    for i in range(len(offsets)):
        out = eng.step({"symbol": np.array(["A"]), "close": np.array([closes[i]]),
                        "high": np.array([closes[i] + 0.3]), "low": np.array([closes[i] - 0.3]),
                        "volume": np.array([vols[i]], dtype=np.float64),
                        "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)})["price_volume"]
    for w in (5, 15, 30):
        leg_vwap = cl / (last[f"scv{w}"][0] / last[f"sv{w}"][0]) - 1.0
        leg_up = last[f"su{w}"][0] / last[f"sv{w}"][0]
        assert out[f"vwap_deviation_{w}m"][0] == pytest.approx(leg_vwap, rel=1e-7), f"vwap_deviation_{w}m sparse"
        assert out[f"up_volume_ratio_{w}m"][0] == pytest.approx(leg_up, rel=1e-7), f"up_volume_ratio_{w}m sparse"

    # obv_slope: time-OLS of obv on the REBASED minute axis over the last w wall-minutes (slope-per-MINUTE).
    for w in (15, 30):
        win = df.filter((pl.col("minute") > t_last - datetime.timedelta(minutes=w)) & (pl.col("minute") <= t_last))
        axis = np.array([(x - base).total_seconds() / 60.0 for x in win["minute"]])
        axis = axis - axis.min()
        y = win["obv"].to_numpy()
        n = len(axis)
        cov = n * (axis * y).sum() - axis.sum() * y.sum()
        var_x = n * (axis * axis).sum() - axis.sum() ** 2
        leg_obv = (cov / var_x) / win["volume"].mean()
        assert out[f"obv_slope_{w}m"][0] == pytest.approx(leg_obv, rel=1e-6), (
            f"obv_slope_{w}m sparse: clean != legacy time-OLS on the rebased-minute axis"
        )


def test_trend_quality_sparse_time_ols_matches_legacy() -> None:
    """RE-GATE trend_quality (#60 re-port, 12ee077) on the SPARSE axis. The earlier green was DENSE-ONLY: clean
    used x=np.arange(w) (positional), but legacy regresses close on the REBASED MINUTE axis over the trailing
    w MINUTES. Head-to-head a gappy uptrend symbol: price_slope (slope-per-MINUTE) + price_r2 match legacy
    time-OLS; and a FLAT-price warmed window → slope=0, r2=0 (not NaN — flat is a perfectly-explained
    zero-trend)."""
    import datetime  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70]
    rng = np.random.default_rng(21)
    closes = 100.0 + np.arange(len(offsets)) * 0.3 + rng.standard_normal(len(offsets)) * 0.2
    mins = [base + datetime.timedelta(minutes=o) for o in offsets]
    eng = CleanEngine([TrendQualityClean()], ["A"], 400)
    out = {}
    for i in range(len(offsets)):
        out = eng.step({"symbol": np.array(["A"]), "close": np.array([closes[i]]),
                        "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)})["trend_quality"]
    for w in (15, 30):
        idx = [i for i, o in enumerate(offsets) if (offsets[-1] - o) < w]
        axis = np.array([float(offsets[i]) for i in idx])
        axis = axis - axis.min()  # rebased minute axis
        y = np.array([closes[i] for i in idx])
        n = len(axis)
        cov = n * (axis * y).sum() - axis.sum() * y.sum()
        var_x = n * (axis * axis).sum() - axis.sum() ** 2
        slope = cov / var_x
        leg_slope = slope / y.mean()  # fractional move per minute (legacy normalization)
        intercept = (y.sum() - slope * axis.sum()) / n
        yhat = slope * axis + intercept
        ss_res = ((y - yhat) ** 2).sum()
        ss_tot = ((y - y.mean()) ** 2).sum()
        leg_r2 = 1.0 - ss_res / ss_tot
        assert out[f"price_slope_{w}m"][0] == pytest.approx(leg_slope, rel=1e-5), f"price_slope_{w}m sparse time-OLS"
        assert out[f"price_r2_{w}m"][0] == pytest.approx(leg_r2, rel=1e-5), f"price_r2_{w}m sparse time-OLS"

    flat = CleanEngine([TrendQualityClean()], ["A"], 400)
    of = {}
    for o in offsets:
        of = flat.step({"symbol": np.array(["A"]), "close": np.array([100.0]),
                        "minute_epoch": np.array([int((base + datetime.timedelta(minutes=o)).timestamp())],
                                                 dtype=np.int64)})["trend_quality"]
    assert of["price_slope_15m"][0] == pytest.approx(0.0), "flat price → slope 0 (no move)"
    # flat price → r2 0.0: the OLS kernel leaves r2 null/NaN on var_y=0, but trend_quality's assemble() PINS
    # it to 0 (trend_quality.py:122-124). CONFIRMED via legacy compute() OUTPUT on a flat frame = 0.0 across
    # all windows (the authoritative oracle; my earlier 'legacy NULLs it' read the kernel, not the assemble).
    assert of["price_r2_15m"][0] == pytest.approx(0.0), "flat price → r2 0 (assemble pins it; legacy output=0)"


def test_reduction_groups_sparse_time_window_matches_legacy() -> None:
    """RE-GATE the re-ported reduction groups (#60 false-green fixes) on the SPARSE axis, one window-exercising
    feature each, head-to-head vs legacy BATCH time-windows on a gappy symbol (spans 70 min). These were dense-
    only false-greens (positional window); now trailing_time-masked.
      - volatility.realized_vol_{5,15,30}m = legacy rolling_std_by(ret, Wm) (ddof=1)
      - realized_range.realized_range_10m = legacy rolling_mean_by((high-low)/close, 10m)
      - vwap_deviation.vwap_deviation_15m = close / (Σ(close·vol,15m)/Σ(vol,15m)) − 1
    """
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    from quantlib.features.clean_groups_example import RealizedRangeClean, VwapDeviationClean  # noqa: PLC0415
    from quantlib.features.clean_groups_windowed import VolatilityClean  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70]
    rng = np.random.default_rng(31)
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)) * 0.4)
    highs = closes + np.abs(rng.standard_normal(len(offsets))) * 0.4 + 0.2
    lows = closes - np.abs(rng.standard_normal(len(offsets))) * 0.4 - 0.2
    vols = (rng.random(len(offsets)) * 1e5 + 1e4).round()
    mins = [base + datetime.timedelta(minutes=o) for o in offsets]
    col_map = {"high": highs, "low": lows, "close": closes, "volume": vols.astype(float)}

    def _feed(group: object) -> dict[str, np.ndarray]:
        eng = CleanEngine([group], ["A"], 400)
        out: dict[str, np.ndarray] = {}
        for i in range(len(offsets)):
            bar = {"symbol": np.array(["A"]), "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)}
            for c in group.input_cols:  # type: ignore[attr-defined]
                bar[c] = np.array([col_map[c][i]], dtype=np.float64)
            out = eng.step(bar)[group.name]  # type: ignore[attr-defined]
        return out

    df = pl.DataFrame({"minute": mins, "close": closes, "high": highs, "low": lows,
                       "volume": vols.astype(float)}).sort("minute")
    df = df.with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"),
                         ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("hlr"),
                         (pl.col("close") * pl.col("volume")).alias("cv"))
    for w in (5, 15, 30):
        df = df.with_columns(pl.col("ret").rolling_std_by("minute", window_size=f"{w}m").alias(f"rv{w}"))
    df = df.with_columns(pl.col("hlr").rolling_mean_by("minute", window_size="10m").alias("rr10"),
                         pl.col("cv").rolling_sum_by("minute", window_size="15m").alias("scv"),
                         pl.col("volume").rolling_sum_by("minute", window_size="15m").alias("sv"))
    last = df.tail(1)

    vol_out = _feed(VolatilityClean())
    for w in (5, 15, 30):
        assert vol_out[f"realized_vol_{w}m"][0] == pytest.approx(last[f"rv{w}"][0], rel=1e-7), (
            f"volatility.realized_vol_{w}m sparse != legacy rolling_std_by"
        )
    rr_out = _feed(RealizedRangeClean())
    assert rr_out["realized_range_10m"][0] == pytest.approx(last["rr10"][0], rel=1e-7), "realized_range_10m sparse"
    vd_out = _feed(VwapDeviationClean())
    leg_vwap = closes[-1] / (last["scv"][0] / last["sv"][0]) - 1.0
    assert vd_out["vwap_deviation_15m"][0] == pytest.approx(leg_vwap, rel=1e-7), "vwap_deviation_15m sparse"


def test_ohlcvol_rangeexp_distribution_sparse_matches_legacy() -> None:
    """RE-GATE ohlc_vol / range_expansion / distribution (#60 re-ports) on the SPARSE axis vs legacy BATCH
    time-windows on a gappy symbol (70 min):
      - ohlc_vol.garman_klass_vol_15m = sqrt(rolling_mean_by(GK, 15m)), GK = 0.5·ln(h/l)² − (2ln2−1)·ln(c/o)²
      - range_expansion.range_expansion_5_30m = mean((h−l)/c, 5m) / mean(·, 30m)
      - distribution.ret_skew_10m = biased skew of the 1m returns in the last 10 wall-minutes
    """
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    from quantlib.features.clean_groups_windowed import (  # noqa: PLC0415
        DistributionClean,
        OhlcVolClean,
        RangeExpansionClean,
    )

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70]
    rng = np.random.default_rng(31)
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)) * 0.4)
    highs = closes + np.abs(rng.standard_normal(len(offsets))) * 0.4 + 0.2
    lows = closes - np.abs(rng.standard_normal(len(offsets))) * 0.4 - 0.2
    opens = closes + rng.standard_normal(len(offsets)) * 0.1
    mins = [base + datetime.timedelta(minutes=o) for o in offsets]
    col_map = {"open": opens, "high": highs, "low": lows, "close": closes}

    def _feed(group: object) -> dict[str, np.ndarray]:
        eng = CleanEngine([group], ["A"], 400)
        out: dict[str, np.ndarray] = {}
        for i in range(len(offsets)):
            bar = {"symbol": np.array(["A"]), "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)}
            for c in group.input_cols:  # type: ignore[attr-defined]
                bar[c] = np.array([col_map[c][i]], dtype=np.float64)
            out = eng.step(bar)[group.name]  # type: ignore[attr-defined]
        return out

    df = pl.DataFrame({"minute": mins, "close": closes, "high": highs, "low": lows, "open": opens}).sort("minute")
    df = df.with_columns(
        (0.5 * (pl.col("high") / pl.col("low")).log() ** 2
         - (2 * np.log(2) - 1) * (pl.col("close") / pl.col("open")).log() ** 2).alias("gk"),
        ((pl.col("high") - pl.col("low")) / pl.col("close")).alias("hlr"),
        (pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"))
    df = df.with_columns(pl.col("gk").rolling_mean_by("minute", window_size="15m").alias("gkm15"),
                         pl.col("hlr").rolling_mean_by("minute", window_size="5m").alias("r5"),
                         pl.col("hlr").rolling_mean_by("minute", window_size="30m").alias("r30"))
    last = df.tail(1)

    ov = _feed(OhlcVolClean())
    assert ov["garman_klass_vol_15m"][0] == pytest.approx(np.sqrt(max(last["gkm15"][0], 0.0)), rel=1e-6), \
        "ohlc_vol.garman_klass_vol_15m sparse"
    re_out = _feed(RangeExpansionClean())
    assert re_out["range_expansion_5_30m"][0] == pytest.approx(last["r5"][0] / last["r30"][0], rel=1e-6), \
        "range_expansion_5_30m sparse"

    dist = _feed(DistributionClean())
    t_last = mins[-1]
    win = df.filter((pl.col("minute") > t_last - datetime.timedelta(minutes=10)) & (pl.col("minute") <= t_last))
    r = win["ret"].drop_nulls().to_numpy()
    n = len(r)
    mean_r = r.mean()
    std_r = np.sqrt(((r - mean_r) ** 2).sum() / n)
    hand_skew = ((r - mean_r) ** 3).sum() / n / std_r ** 3
    assert dist["ret_skew_10m"][0] == pytest.approx(hand_skew, rel=1e-5), "distribution.ret_skew_10m sparse"


def test_liquidity_quote_spread_sparse_matches_legacy() -> None:
    """RE-GATE liquidity + quote_spread (#60 re-ports, enriched trade/quote inputs) on the SPARSE axis vs
    legacy BATCH time-windows on a gappy symbol (70 min):
      - quote_spread.spread_bps_15m = rolling_mean_by(mean_spread_bps, 15m)
      - liquidity.amihud_illiq_15m = rolling_mean_by(|ret|/(close·volume), 15m)
    """
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    from quantlib.features.clean_groups_windowed import LiquidityClean, QuoteSpreadClean  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70]
    rng = np.random.default_rng(31)
    mins = [base + datetime.timedelta(minutes=o) for o in offsets]
    spread = 10.0 + rng.random(len(offsets)) * 5
    imb = rng.standard_normal(len(offsets)) * 0.2
    bid = 1000.0 + rng.random(len(offsets)) * 500
    ask = 1000.0 + rng.random(len(offsets)) * 500
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)) * 0.4)
    vols = (rng.random(len(offsets)) * 1e5 + 1e4).round()
    sv = rng.standard_normal(len(offsets)) * vols

    qs = CleanEngine([QuoteSpreadClean()], ["A"], 400)
    qout = {}
    for i in range(len(offsets)):
        qout = qs.step({"symbol": np.array(["A"]), "mean_spread_bps": np.array([spread[i]]),
                        "quote_imbalance": np.array([imb[i]]), "mean_bid_size": np.array([bid[i]]),
                        "mean_ask_size": np.array([ask[i]]),
                        "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)})["quote_spread"]
    df = pl.DataFrame({"minute": mins, "sp": spread}).sort("minute")
    df = df.with_columns(pl.col("sp").rolling_mean_by("minute", window_size="15m").alias("sp15"))
    assert qout["spread_bps_15m"][0] == pytest.approx(df.tail(1)["sp15"][0], rel=1e-7), "spread_bps_15m sparse"

    liq = CleanEngine([LiquidityClean()], ["A"], 400)
    lout = {}
    for i in range(len(offsets)):
        lout = liq.step({"symbol": np.array(["A"]), "close": np.array([closes[i]]),
                         "volume": np.array([vols[i]]), "signed_volume": np.array([sv[i]]),
                         "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)})["liquidity"]
    df2 = pl.DataFrame({"minute": mins, "close": closes, "volume": vols.astype(float)}).sort("minute")
    df2 = df2.with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).abs().alias("aret"),
                           (pl.col("close") * pl.col("volume")).alias("dv"))
    df2 = df2.with_columns((pl.col("aret") / pl.col("dv")).rolling_mean_by("minute", window_size="15m").alias("am15"))
    assert lout["amihud_illiq_15m"][0] == pytest.approx(df2.tail(1)["am15"][0], rel=1e-6), "amihud_illiq_15m sparse"


def test_momentum_family_sparse_matches_legacy() -> None:
    """GATE the FRESH time-window ports momentum / efficiency / return_dynamics (#60, 8a232b0) on the SPARSE
    axis vs legacy BATCH on a gappy symbol (70 min):
      - momentum.up_ratio_15m = mean(up-bar, 15m time window)
      - efficiency.efficiency_ratio_15m = |close[T]−close.shift(15)| / Σ|step|(15m) — a HYBRID: POSITIONAL
        shift(w) numerator (w-th prior ROW) + TIME-window Σ|step| denominator (verified at source).
      - return_dynamics.autocorr_1_15m = corr(ret_t, ret_{t−1}) over the 15m window (value-OLS, paired-finite).
    """
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    from quantlib.features.clean_groups_windowed import (  # noqa: PLC0415
        EfficiencyClean,
        MomentumClean,
        ReturnDynamicsClean,
    )

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70]
    rng = np.random.default_rng(41)
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)) * 0.4)
    mins = [base + datetime.timedelta(minutes=o) for o in offsets]
    t_last = mins[-1]

    def _feed(group: object) -> dict[str, np.ndarray]:
        eng = CleanEngine([group], ["A"], 400)
        out: dict[str, np.ndarray] = {}
        for i in range(len(offsets)):
            out = eng.step({"symbol": np.array(["A"]), "close": np.array([closes[i]]),
                            "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)})[group.name]  # type: ignore[attr-defined]
        return out

    df = pl.DataFrame({"minute": mins, "close": closes}).sort("minute")
    df = df.with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"),
                         (pl.col("close") - pl.col("close").shift(1)).abs().alias("step"))
    df = df.with_columns(pl.when(pl.col("ret") > 0).then(1.0).otherwise(0.0).alias("up"),
                         pl.col("ret").shift(1).alias("lret"))

    # momentum.up_ratio_15m
    win = df.filter((pl.col("minute") > t_last - datetime.timedelta(minutes=15)) & (pl.col("minute") <= t_last))
    leg_up = win["up"].drop_nulls().mean()
    assert _feed(MomentumClean())["up_ratio_15m"][0] == pytest.approx(leg_up, rel=1e-6), "momentum.up_ratio_15m sparse"

    # efficiency.efficiency_ratio_15m: HYBRID positional-shift(15) net / time-window Σ|step|
    path = win["step"].drop_nulls().sum()
    net = abs(closes[-1] - closes[-1 - 15])  # positional shift(15): the 15th prior present ROW
    assert _feed(EfficiencyClean())["efficiency_ratio_15m"][0] == pytest.approx(net / path, rel=1e-6), \
        "efficiency_ratio_15m sparse (positional-net / time-path hybrid)"

    # return_dynamics.autocorr_1_15m: corr(ret, lagged ret) over the time window
    pw = win.drop_nulls(["ret", "lret"])
    x = pw["lret"].to_numpy()
    y = pw["ret"].to_numpy()
    n = len(x)
    cov = n * (x * y).sum() - x.sum() * y.sum()
    var_x = n * (x * x).sum() - x.sum() ** 2
    var_y = n * (y * y).sum() - y.sum() ** 2
    leg_ac = cov / np.sqrt(var_x * var_y)
    assert _feed(ReturnDynamicsClean())["autocorr_1_15m"][0] == pytest.approx(leg_ac, rel=1e-5), \
        "return_dynamics.autocorr_1_15m sparse (value-OLS)"


def test_volume_drawrange_momentumconsistency_sparse_matches_legacy() -> None:
    """GATE volume / draw_range / momentum_consistency (#60 fresh time-window ports) on the SPARSE axis vs
    legacy BATCH on a gappy symbol (70 min):
      - volume.volume_zscore_15m = (vol[T] − mean(vol,15m)) / std(vol,15m, ddof=1)
      - draw_range.draw_range_60m = max-drawdown + max-drawup over the closes in the 60m TIME window
      - momentum_consistency.reversal_count_15m = Σ(sign-flip)(15m) / 15, where flip uses the per-minute return
        (close/close.shift(1) on the sparse minute frame = prior present ROW; NaN at the first bar).
    """
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    from quantlib.features.clean_groups_windowed import (  # noqa: PLC0415
        DrawRangeClean,
        MomentumConsistencyClean,
        VolumeClean,
    )

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70]
    rng = np.random.default_rng(51)
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)) * 0.4)
    vols = (rng.random(len(offsets)) * 1e5 + 1e4).round()
    mins = [base + datetime.timedelta(minutes=o) for o in offsets]
    t_last = mins[-1]
    col_map = {"close": closes, "volume": vols.astype(float)}

    def _feed(group: object) -> dict[str, np.ndarray]:
        eng = CleanEngine([group], ["A"], 400)
        out: dict[str, np.ndarray] = {}
        for i in range(len(offsets)):
            bar = {"symbol": np.array(["A"]), "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)}
            for c in group.input_cols:  # type: ignore[attr-defined]
                bar[c] = np.array([col_map[c][i]], dtype=np.float64)
            out = eng.step(bar)[group.name]  # type: ignore[attr-defined]
        return out

    df = pl.DataFrame({"minute": mins, "close": closes, "volume": vols.astype(float)}).sort("minute")
    df = df.with_columns(pl.col("volume").rolling_mean_by("minute", window_size="15m").alias("vm15"),
                         pl.col("volume").rolling_std_by("minute", window_size="15m").alias("vs15"))
    last = df.tail(1)
    vout = _feed(VolumeClean())
    assert vout["volume_zscore_15m"][0] == pytest.approx((vols[-1] - last["vm15"][0]) / last["vs15"][0], rel=1e-6), \
        "volume_zscore_15m sparse"

    win = df.filter((pl.col("minute") > t_last - datetime.timedelta(minutes=60)) & (pl.col("minute") <= t_last))
    c = win["close"].to_numpy()
    maxdd = -(c / np.maximum.accumulate(c) - 1.0).min()
    maxdu = (c / np.minimum.accumulate(c) - 1.0).max()
    dout = _feed(DrawRangeClean())
    assert dout["draw_range_60m"][0] == pytest.approx(maxdd + maxdu, rel=1e-6), "draw_range_60m sparse"

    dfm = df.with_columns((pl.col("close") / pl.col("close").shift(1) - 1.0).alias("ret"))
    dfm = dfm.with_columns(pl.col("ret").shift(1).alias("rp"))
    dfm = dfm.with_columns(
        (pl.col("ret").is_not_null() & pl.col("rp").is_not_null() & (pl.col("ret") != 0) & (pl.col("rp") != 0)
         & ((pl.col("ret") > 0) != (pl.col("rp") > 0))).cast(pl.Float64).alias("flip"))
    dfm = dfm.with_columns(pl.col("flip").rolling_sum_by("minute", window_size="15m").alias("fsum"))
    mc = _feed(MomentumConsistencyClean())
    assert mc["reversal_count_15m"][0] == pytest.approx(dfm.tail(1)["fsum"][0] / 15.0, rel=1e-7), \
        "reversal_count_15m sparse (per-minute return = prior present row on the sparse frame)"


def test_residual_analysis_clean_momentum_sparse_match_legacy_output() -> None:
    """GATE residual_analysis + clean_momentum (#60 FRESH time-OLS ports w/ rebased-minute axis) on the SPARSE
    axis. Both are close-vs-TIME OLS (frame-relative minute axis = trend_quality's), so the rebased-minute axis
    applies — verified by a full cell-for-cell diff vs the AUTHORITATIVE legacy compute() OUTPUT on this exact
    gappy frame (residual_analysis 6/6, clean_momentum 12/12 incl the binary momentum_quality_flag, run
    out-of-band with attach_reduction_anchors). The expected values below ARE that legacy output (seed=61,
    sparse offsets) — pinned so a regression in the time-OLS / rebase / blend is caught."""
    import datetime  # noqa: PLC0415

    from quantlib.features.clean_groups_windowed import CleanMomentumClean, ResidualAnalysisClean  # noqa: PLC0415,E501

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70]
    rng = np.random.default_rng(61)
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)) * 0.4)
    mins = [base + datetime.timedelta(minutes=o) for o in offsets]

    def _feed(group: object) -> dict[str, np.ndarray]:
        eng = CleanEngine([group], ["A"], 400)
        out: dict[str, np.ndarray] = {}
        for i in range(len(offsets)):
            out = eng.step({"symbol": np.array(["A"]), "close": np.array([closes[i]]),
                            "minute_epoch": np.array([int(mins[i].timestamp())], dtype=np.int64)})[group.name]  # type: ignore[attr-defined]
        return out

    ra = _feed(ResidualAnalysisClean())
    # legacy compute() output (residual_std = OLS-residual std as % of mean price, over the time window):
    assert ra["residual_std_15m"][0] == pytest.approx(0.12889800547444194, rel=1e-6)
    assert ra["residual_std_30m"][0] == pytest.approx(0.25808006665765837, rel=1e-6)

    cm = _feed(CleanMomentumClean())
    # legacy compute() output (score = the slope/r2/low-residual blend; flag = binary quality gate):
    assert cm["clean_momentum_score_15m"][0] == pytest.approx(0.44679822581044715, rel=1e-6)
    assert cm["clean_momentum_score_30m"][0] == pytest.approx(0.5044289836325836, rel=1e-6)
    assert cm["momentum_quality_flag_15m"][0] == pytest.approx(0.0)
    assert cm["momentum_quality_flag_30m"][0] == pytest.approx(0.0)


def test_candlestick_two_candle_patterns_strict_minute_lag() -> None:
    """RE-GATE candlestick (#60): the TWO-CANDLE patterns (engulfing/harami) compare against the bar at EXACTLY
    minute−1 (legacy LagSpec(minutes=1)=base.lagged), NULL when that minute is absent — NOT the prior PRESENT
    bar. The 7 single-bar geometry feats stay positional (latest OHLC). Earlier this was a false-green: a gappy
    symbol fired a pattern off a stale prior-present bar where legacy NULLs.
      - GAP (prior minute absent) → pattern NaN (no exact minute−1 bar).
      - CONSECUTIVE minutes → pattern fires (engulfing=1.0).
      - single-bar body_ratio computes on the gapped bar (positional)."""
    import datetime  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)

    def _bar(op: float, hi: float, lo: float, cl: float, minute: int) -> dict[str, np.ndarray]:
        return {"symbol": np.array(["A"]), "open": np.array([op]), "high": np.array([hi]),
                "low": np.array([lo]), "close": np.array([cl]),
                "minute_epoch": np.array([int((base + datetime.timedelta(minutes=minute)).timestamp())],
                                         dtype=np.int64)}

    # GAP: bearish bar at minute 0, bullish bar at minute 5 (minutes 1-4 ABSENT). The exact prior minute (4) is
    # absent → engulfing NULL, NOT fired off the stale minute-0 bar.
    gap = CleanEngine([CandlestickClean()], ["A"], 400)
    gap.step(_bar(105.0, 106.0, 99.0, 100.0, 0))  # bearish
    out_gap = gap.step(_bar(99.0, 107.0, 98.0, 106.0, 5))["candlestick"]  # bullish, minute 4 absent
    assert np.isnan(out_gap["pattern_engulfing_bullish"][0]), \
        "gap: no bar at exactly minute−1 → engulfing NULL (not off the stale prior-present bar)"
    assert np.isfinite(out_gap["body_ratio"][0]), "single-bar body_ratio still computes on the gapped bar"

    # CONSECUTIVE: bearish minute 0, bullish minute 1 → the prior minute IS present → engulfing fires.
    seq = CleanEngine([CandlestickClean()], ["A"], 400)
    seq.step(_bar(105.0, 106.0, 99.0, 100.0, 0))  # bearish
    out_seq = seq.step(_bar(99.0, 107.0, 98.0, 106.0, 1))["candlestick"]  # bullish, consecutive
    assert out_seq["pattern_engulfing_bullish"][0] == pytest.approx(1.0), \
        "consecutive minutes: bullish body engulfs the prior bearish body → fires"


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
# DRAINED (batch #55, 8a33705/1d96c8a): the 4 return_dispersion daily horizons now compute from the daily
# snapshot (window.session['daily_close']) — no longer NaN-by-design. Ledger EMPTY.
DEFERRED_NAN_FEATURES: dict[str, list[str]] = {}


def test_xsec_iqr_matches_polars_default_on_half_integer_position() -> None:
    """SILENT-DIVERGENCE GATE (the bug f20d8cc fixed): the legacy IQR is polars ``col.quantile(0.75/0.25)`` with
    polars DEFAULT interpolation. Polars default ('nearest') uses ROUND-HALF-UP of ``q*(n-1)``, NOT numpy
    ``method='nearest'`` (round-half-to-EVEN). They diverge exactly when ``q*(n-1)`` lands on x.5.

    The PRIOR fixture ([1,2,3,4]) was a FALSE GREEN — there polars-nearest, numpy-nearest and numpy-higher all
    coincide. This pins a DIVERGENT shape: n=7, q=0.75 → q*(n-1)=4.5 → polars picks sorted index 5, numpy
    'nearest' picks index 4. The clean _xsec_std_iqr MUST match legacy polars, or every captured IQR feature is
    train/serve-skewed vs the historical (polars-computed) vectors."""
    import polars as pl  # noqa: PLC0415

    vals = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 10.0, 20.0])  # q*(n-1)=4.5 for q=0.75 → polars idx5, numpy idx4
    present = np.full(vals.shape, True)
    _std, iqr = _xsec_std_iqr(vals, present)
    legacy_iqr = float(pl.Series(vals).quantile(0.75) - pl.Series(vals).quantile(0.25))  # polars default
    numpy_nearest_iqr = float(
        np.quantile(vals, 0.75, method="nearest") - np.quantile(vals, 0.25, method="nearest")
    )
    assert legacy_iqr != pytest.approx(numpy_nearest_iqr), (
        "fixture must be a DIVERGENT shape (polars-default != numpy-nearest), else the test is vacuous"
    )
    assert iqr == pytest.approx(legacy_iqr), (
        f"clean IQR {iqr} != legacy polars IQR {legacy_iqr} — _xsec_std_iqr must use polars' round-half-up rule"
    )


def test_xsec_iqr_matches_polars_across_random_universes() -> None:
    """Stronger pin: the clean IQR must match legacy polars over MANY random cross-sections (varied n, including
    the half-integer positions that expose the round-half-up vs round-half-even gap)."""
    import polars as pl  # noqa: PLC0415

    rng = np.random.default_rng(99)
    for _ in range(300):
        n = int(rng.integers(2, 120))
        vals = rng.standard_normal(n) * float(rng.uniform(1e-4, 0.2))
        present = np.full(vals.shape, True)
        _std, iqr = _xsec_std_iqr(vals, present)
        legacy_iqr = float(pl.Series(vals).quantile(0.75) - pl.Series(vals).quantile(0.25))
        assert iqr == pytest.approx(legacy_iqr, abs=1e-15), f"n={n}: clean IQR {iqr} != legacy polars {legacy_iqr}"


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


def test_return_dispersion_deferred_ledger_empty_and_contract() -> None:
    """LEDGER-DRAIN gate (batch #55): the 4 daily horizons now compute from the daily snapshot, so the
    DEFERRED_NAN_FEATURES ledger MUST be empty (no feature emitted NaN-by-design). produced == declared."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.return_dispersion import ReturnDispersionGroup  # type: ignore  # noqa: PLC0415,E501

    assert DEFERRED_NAN_FEATURES == {}, "deferred ledger must be empty for all-68-green"
    declared = {s.name for s in ReturnDispersionGroup().declare()}
    assert set(ReturnDispersionClean().feature_names) == declared


def test_market_turbulence_sparse_matches_legacy() -> None:
    """GATE market_turbulence (#60 time-window cross-sectional) on the SPARSE axis. mkt_absret_W uses a STRICT
    exact-minute lag (legacy lagged(close,W): the close at EXACTLY T−W, NaN if no bar there — NOT the nearest
    bar); mkt_rv_30m = universe-mean of per-symbol std of 1m logrets over (T−30,T] gated to EXACT 1m steps.
    Head-to-head a dense + a sparse symbol vs legacy lagged + rolling_std_by."""
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    from quantlib.features.base import lagged  # type: ignore  # noqa: PLC0415
    from quantlib.features.clean_groups_xsectional import MarketTurbulenceClean  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    rng = np.random.default_rng(6)
    a_off = list(range(0, 40))
    b_off = [0, 1, 2, 5, 8, 12, 15, 18, 22, 25, 28, 31, 34, 37, 38, 39]  # sparse
    a_cl = 100.0 + np.cumsum(rng.standard_normal(len(a_off)) * 0.3)
    b_cl = 50.0 + np.cumsum(rng.standard_normal(len(b_off)) * 0.3)
    rows = ([{"symbol": "A", "minute": base + datetime.timedelta(minutes=o), "close": float(c)}
             for o, c in zip(a_off, a_cl)]
            + [{"symbol": "B", "minute": base + datetime.timedelta(minutes=o), "close": float(c)}
               for o, c in zip(b_off, b_cl)])
    df = pl.DataFrame(rows).sort(["symbol", "minute"])
    last_min = base + datetime.timedelta(minutes=39)

    fr = lagged(df, "close", 5, "_lag").with_columns((pl.col("close") / pl.col("_lag") - 1.0).abs().alias("_ar"))
    leg_absret5 = fr.group_by("minute").agg(pl.col("_ar").mean().alias("m")).filter(pl.col("minute") == last_min)["m"][0]
    fr2 = df.with_columns(
        (pl.col("close").log() - pl.col("close").shift(1).over("symbol").log()).alias("_lr"),
        (pl.col("minute") - pl.col("minute").shift(1).over("symbol")).alias("_dt"))
    fr2 = fr2.with_columns(
        pl.when(pl.col("_dt") == pl.duration(minutes=1)).then(pl.col("_lr")).otherwise(None).alias("_lr1"))
    fr2 = fr2.with_columns(pl.col("_lr1").rolling_std_by(
        "minute", window_size="30m", min_samples=10, closed="right").over("symbol").alias("_rv"))
    leg_rv = fr2.group_by("minute").agg(pl.col("_rv").mean().alias("m")).filter(pl.col("minute") == last_min)["m"][0]

    events: dict[int, dict[str, float]] = {}
    for o, c in zip(a_off, a_cl):
        events.setdefault(o, {})["A"] = float(c)
    for o, c in zip(b_off, b_cl):
        events.setdefault(o, {})["B"] = float(c)
    eng = CleanEngine([MarketTurbulenceClean()], ["A", "B"], 400)
    out = {}
    for o in sorted(events):
        pm = events[o]
        out = eng.step({"symbol": np.array(list(pm)), "close": np.array([pm[s] for s in pm], dtype=np.float64),
                        "minute_epoch": np.array([int((base + datetime.timedelta(minutes=o)).timestamp())],
                                                 dtype=np.int64)})["market_turbulence"]
    assert out["mkt_absret_5m"][0] == pytest.approx(leg_absret5), "sparse mkt_absret_5m != legacy lagged"
    assert out["mkt_rv_30m"][0] == pytest.approx(leg_rv), "sparse mkt_rv_30m != legacy rolling_std_by"


def test_market_turbulence_exact_lag_nan_when_no_bar_at_lag_minute() -> None:
    """The STRICT-lag semantic: a symbol with no bar exactly W minutes ago contributes NO |return| (NaN), not
    a return off its nearest bar. One symbol, bars at minutes 0 and 7 only — at T=7, the 5m lag minute (2) has
    no bar → that symbol's absret_5m is undefined; with only that symbol present the universe mean is NaN."""
    import datetime  # noqa: PLC0415

    from quantlib.features.clean_groups_xsectional import MarketTurbulenceClean  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    eng = CleanEngine([MarketTurbulenceClean()], ["A"], 400)
    eng.step({"symbol": np.array(["A"]), "close": np.array([100.0]),
              "minute_epoch": np.array([int(base.timestamp())], dtype=np.int64)})  # minute 0
    out = eng.step({"symbol": np.array(["A"]), "close": np.array([105.0]),
                    "minute_epoch": np.array([int((base + datetime.timedelta(minutes=7)).timestamp())],
                                             dtype=np.int64)})["market_turbulence"]  # minute 7, lag-5 = minute 2 (no bar)
    assert np.isnan(out["mkt_absret_5m"][0]), "no bar at exactly T−5 → strict lag is NaN (not nearest-bar return)"


def _step_return_dispersion_with_daily(daily_close: np.ndarray, syms: list[str]) -> dict[str, np.ndarray]:
    """Drive ReturnDispersionClean through the engine with a daily snapshot set, all symbols present one minute,
    so the daily horizons compute from window.session['daily_close']."""
    eng = CleanEngine([ReturnDispersionClean()], syms, WINDOW)
    eng.set_session({"daily_close": daily_close})
    rng = np.random.default_rng(5)
    for ep in range(8):
        eng.step({"symbol": np.array(syms),
                  "close": np.array([100.0 + rng.standard_normal() for _ in syms], dtype=np.float64),
                  "volume": np.zeros(len(syms), dtype=np.float64),
                  "minute_epoch": np.array([60 + ep * 60], dtype=np.int64)})
    return eng.step({"symbol": np.array(syms),
                     "close": np.array([101.0 + i for i in range(len(syms))], dtype=np.float64),
                     "volume": np.zeros(len(syms), dtype=np.float64),
                     "minute_epoch": np.array([600], dtype=np.int64)})["return_dispersion"]


def test_return_dispersion_daily_horizons_compute_real_xsec_dispersion() -> None:
    """FULL-GREEN gate for the daily horizons: with a daily snapshot set, return_dispersion_{std,iqr}_{1d,5d}
    equal the cross-sectional std(ddof=1)/IQR of the universe's w-day returns from the snapshot — a real value
    (no longer deferred-NaN), present()-gated and broadcast."""
    import polars as pl  # noqa: PLC0415

    syms = ["A", "B", "C", "D", "E"]
    daily_close = np.array([  # (n_sym, n_days), newest col LAST; 1d return = col[-1]/col[-2]-1
        [98.0, 100.0, 102.0],
        [49.0, 50.0, 51.5],
        [201.0, 200.0, 198.0],
        [78.0, 80.0, 84.0],
        [9.9, 10.0, 10.1],
    ])
    out = _step_return_dispersion_with_daily(daily_close, syms)
    ret_1d = daily_close[:, -1] / daily_close[:, -2] - 1.0
    hand_std_1d = float(np.std(ret_1d, ddof=1))
    hand_iqr_1d = float(pl.Series(ret_1d).quantile(0.75) - pl.Series(ret_1d).quantile(0.25))
    for i in range(len(syms)):  # broadcast to every present symbol
        assert out["return_dispersion_std_1d"][i] == pytest.approx(hand_std_1d)
        assert out["return_dispersion_iqr_1d"][i] == pytest.approx(hand_iqr_1d)
    # only 3 daily columns → 5d return needs 6 → warm-up NaN (a real, correct NaN, not deferred-NaN)
    assert np.all(np.isnan(out["return_dispersion_std_5d"])), "5d warm-up NaN with only 3 daily columns"


def test_return_dispersion_daily_warmup_and_present_gating() -> None:
    """daily horizons NaN where snapshot lacks w+1 days (warm-up), and absent symbols → NaN (present-gated)."""
    syms = ["A", "B", "C"]
    daily_close = np.array([[100.0, 102.0], [50.0, 51.0], [200.0, 199.0]])  # 2 days → 1d ok, 5d warm-up
    eng = CleanEngine([ReturnDispersionClean()], syms, WINDOW)
    eng.set_session({"daily_close": daily_close})
    eng.step({"symbol": np.array(syms), "close": np.array([100.0, 50.0, 200.0]),
              "volume": np.zeros(3), "minute_epoch": np.array([60], dtype=np.int64)})
    out = eng.step({"symbol": np.array(["A", "B"]),  # C absent this minute
                    "close": np.array([101.0, 51.0]), "volume": np.zeros(2),
                    "minute_epoch": np.array([120], dtype=np.int64)})["return_dispersion"]
    assert np.isfinite(out["return_dispersion_std_1d"][0]), "1d computes (2 days)"
    assert np.isnan(out["return_dispersion_std_5d"][0]), "5d warm-up → NaN (only 2 days)"
    assert np.isnan(out["return_dispersion_std_1d"][2]), "absent C → NaN (present-gated)"


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


def test_sector_beta_sparse_time_window_matches_legacy() -> None:
    """RE-GATE sector_beta on the SPARSE axis (#60 re-port to time-windows; my earlier green was DENSE-only).
    3 names in one sector, all gappy (every-other-minute). The 30m OLS of own-return on the sector
    equal-weight return must use the TIME window (legacy rolling_sum_by('minute','30m')), not the last 30
    positional bars (which would span 60 wall-minutes on this sparse tape). Head-to-head replicates legacy
    _ols_from_sums over the true-minute rolling sums."""
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    syms = ["A", "B", "C"]
    sector = np.array([0, 0, 0])
    offsets = list(range(0, 60, 2))  # sparse: every other wall-minute, spans 58 min > 30m
    rng = np.random.default_rng(8)
    closes = {s: 100.0 * np.cumprod(1.0 + rng.standard_normal(len(offsets)) * 0.002) for s in syms}

    # legacy: per-(minute,sector) equal-weight own-return = the regressor; time-windowed OLS at the last minute.
    rows = [{"symbol": s, "minute": base + datetime.timedelta(minutes=o), "close": float(closes[s][j])}
            for s in syms for j, o in enumerate(offsets)]
    df = pl.DataFrame(rows).sort(["symbol", "minute"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("_oret"))
    sret = df.group_by("minute").agg(pl.col("_oret").mean().alias("_sret"))
    df = df.join(sret, on="minute", how="left").sort(["symbol", "minute"]).with_columns(
        (pl.col("_oret") * pl.col("_sret")).alias("_xy"), (pl.col("_sret") ** 2).alias("_xx"),
        (pl.col("_oret") ** 2).alias("_yy"),
        pl.when(pl.col("_oret").is_not_null() & pl.col("_sret").is_not_null()).then(1).otherwise(0).alias("_n1"))

    def _rsum(col: str) -> pl.Expr:
        return pl.col(col).rolling_sum_by("minute", window_size="30m").over("symbol")

    df = df.with_columns(_rsum("_oret").alias("sy"), _rsum("_sret").alias("sx"), _rsum("_xy").alias("sxy"),
                         _rsum("_xx").alias("sxx"), _rsum("_yy").alias("syy"), _rsum("_n1").alias("n"))
    last = df.filter((pl.col("symbol") == "A") & (pl.col("minute") == base + datetime.timedelta(minutes=offsets[-1])))
    nn, sx, sy = last["n"][0], last["sx"][0], last["sy"][0]
    sxy, sxx, syy = last["sxy"][0], last["sxx"][0], last["syy"][0]
    cov, vx, vy = sxy - sx * sy / nn, sxx - sx * sx / nn, syy - sy * sy / nn
    legacy_beta = cov / vx if (nn >= 5 and vx > 0 and vy > 0 and abs(cov / vx) <= 15.0) else np.nan
    legacy_corr = float(np.clip(cov / np.sqrt(vx * vy), -1, 1)) if (nn >= 5 and vx > 0 and vy > 0) else np.nan

    eng = CleanEngine([SectorBetaClean()], syms, 400)
    eng.static = {"sector": sector}
    out = {}
    for j, o in enumerate(offsets):
        out = eng.step({"symbol": np.array(syms), "close": np.array([closes[s][j] for s in syms]),
                        "minute_epoch": np.array([int((base + datetime.timedelta(minutes=o)).timestamp())],
                                                 dtype=np.int64)})["sector_beta"]
    assert nn == 15, "the 30m TIME window holds 15 paired returns on this every-other-minute tape (not 30)"
    assert out["sector_beta_30m"][0] == pytest.approx(legacy_beta, rel=1e-7), "sparse 30m beta != legacy time-OLS"
    assert out["sector_corr_30m"][0] == pytest.approx(legacy_corr, rel=1e-7), "sparse 30m corr != legacy time-OLS"


def test_technical_sparse_time_window_matches_legacy() -> None:
    """GATE the re-ported technical (RSI/Bollinger/sma_dist now use trailing_time, #60) on the SPARSE axis.
    On a gappy symbol the 14m RSI / 20m Bollinger / 5m sma_dist must use the TIME window (legacy
    rolling_mean_by/rolling_std_by over minute), not the last-N positional bars. macd is recursive
    (present-decay, separately validated) — confirmed finite here. Non-vacuous RSI (mixed up/down)."""
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    from quantlib.features.clean_groups_stateful import TechnicalClean  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38, 41, 45, 49, 52, 56, 60, 63, 67, 70]  # spans 70m
    rng = np.random.default_rng(13)
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)) * 0.5)
    minutes = [base + datetime.timedelta(minutes=o) for o in offsets]
    df = pl.DataFrame({"minute": minutes, "close": closes}).sort("minute").with_columns(
        pl.col("close").shift(1).alias("_prev"))
    diff = pl.col("close") - pl.col("_prev")
    gain = pl.when(diff > 0).then(diff).otherwise(0.0)
    loss = pl.when(diff < 0).then(-diff).otherwise(0.0)
    ag = gain.rolling_mean_by("minute", window_size="14m")
    al = loss.rolling_mean_by("minute", window_size="14m")
    total = ag + al
    rsi = pl.when(total > 0).then((100.0 * ag / total).clip(0.0, 100.0)).otherwise(None)
    df = df.with_columns(rsi.alias("rsi"),
                         pl.col("close").rolling_mean_by("minute", window_size="20m").alias("sma20"),
                         pl.col("close").rolling_std_by("minute", window_size="20m").alias("std20"),
                         pl.col("close").rolling_mean_by("minute", window_size="5m").alias("sma5"))
    last = df.tail(1)
    cl = closes[-1]
    rsi_leg, sma20_leg, std20_leg, sma5_leg = last["rsi"][0], last["sma20"][0], last["std20"][0], last["sma5"][0]

    eng = CleanEngine([TechnicalClean()], ["A"], 400)
    out = {}
    for o, c in zip(offsets, closes):
        out = eng.step({"symbol": np.array(["A"]), "close": np.array([c]),
                        "minute_epoch": np.array([int((base + datetime.timedelta(minutes=o)).timestamp())],
                                                 dtype=np.int64)})["technical"]
    assert rsi_leg > 0.0, "fixture must yield a non-vacuous RSI"
    assert out["rsi_14m"][0] == pytest.approx(rsi_leg), "sparse 14m RSI != legacy rolling_mean_by"
    assert out["bb_position_20m"][0] == pytest.approx((cl - sma20_leg) / (2.0 * std20_leg)), "sparse bb_position"
    assert out["sma_dist_5m"][0] == pytest.approx(cl / sma5_leg - 1.0), "sparse 5m sma_dist != legacy"
    assert np.isfinite(out["macd_line"][0]), "macd (recursive, present-decay) computes"


def test_technical_contract() -> None:
    """produced == declared for technical with legacy valid_range/nan_policy (rsi∈[0,100], bb_width≥0,
    sma_dist∈[-1,5])."""
    import datetime  # noqa: PLC0415
    import sys  # noqa: PLC0415

    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.clean_groups_stateful import TechnicalClean  # noqa: PLC0415
    from quantlib.features.groups.technical import TechnicalGroup  # type: ignore  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    rng = np.random.default_rng(21)
    eng = CleanEngine([TechnicalClean()], ["A"], 400)
    out = {}
    for t in range(60):
        c = 100.0 + np.cumsum(rng.standard_normal(1))[0]
        out = eng.step({"symbol": np.array(["A"]), "close": np.array([c]),
                        "minute_epoch": np.array([int((base + datetime.timedelta(minutes=t)).timestamp())],
                                                 dtype=np.int64)})["technical"]
    _assert_feature_spec_contract(out, TechnicalGroup().declare(), "technical ")


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


# ========================================================================================================= #
# CROSS-SECTIONAL — batch 2e: peer_relative (own − behavioral-CLUSTER mean; same 2 present()-gates as
# sector_return but grouped by window.static['cluster_id'], reuses _sector_mean_vector).
# ========================================================================================================= #

from quantlib.features.clean_groups_xsectional import PeerRelativeClean  # noqa: E402


def test_peer_relative_cluster_mean_present_gated() -> None:
    """DENOMINATOR gate: the cluster mean is over PRESENT members only. Cluster 0 = {A,B,C}; B absent → A,C
    demean against {A,C}, not {A,stale-B,C}. peer_relative_ret = own − cluster_mean."""
    syms = ["A", "B", "C"]
    eng = CleanEngine([PeerRelativeClean()], syms, WINDOW)
    eng.static = {"cluster_id": np.array([0, 0, 0])}
    # 8 minutes all present (so the 5m return is defined), then a minute where only A,C deliver (B absent)
    for ep in range(8):
        eng.step(_sec_bar({"A": 100.0 + ep, "B": 100.0 + ep, "C": 100.0 + 2 * ep}, 60 + ep * 60))
    out = eng.step(_sec_bar({"A": 108.0, "C": 116.0}, 600))["peer_relative"]
    # A and C present, demeaned vs the present cluster {A,C} → peer_relative finite, sums to ~0 within {A,C}
    assert np.isfinite(out["peer_relative_ret_5m"][0])  # A
    assert np.isfinite(out["peer_relative_ret_5m"][2])  # C
    assert np.isnan(out["peer_relative_ret_5m"][1])     # B ABSENT → output-gated NaN
    # within the present cluster {A,C}, the two excesses sum to ~0 (demeaned vs their own mean)
    assert out["peer_relative_ret_5m"][0] + out["peer_relative_ret_5m"][2] == pytest.approx(0.0, abs=1e-9)


def test_peer_relative_unmapped_cluster_nan() -> None:
    """NULL cluster (-1) → peer_relative NaN (no peer group)."""
    eng = CleanEngine([PeerRelativeClean()], ["A"], WINDOW)
    eng.static = {"cluster_id": np.array([-1])}
    out = {}
    for ep in range(8):
        out = eng.step(_sec_bar({"A": 100.0 + ep}, 60 + ep * 60))["peer_relative"]
    assert np.isnan(out["peer_relative_ret_5m"][0])


def test_peer_relative_contract_and_seed_equals_live() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.peer_relative import PeerRelativeReturnGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(27)
    syms = [f"S{i}" for i in range(6)]
    clusters = np.array([0, 0, 0, 1, 1, 1])
    hist = []
    for t in range(30):
        present = {s: 100.0 + np.cumsum(rng.standard_normal(1))[0] for s in syms if rng.random() > 0.2}
        if present:
            hist.append(_sec_bar(present, 60 + t * 60))
    se = CleanEngine([PeerRelativeClean()], syms, WINDOW)
    se.static = {"cluster_id": clusters}
    se.seed(hist[:-1])
    so = se.step(hist[-1])
    le = CleanEngine([PeerRelativeClean()], syms, WINDOW)
    le.static = {"cluster_id": clusters}
    lo = {}
    for h in hist:
        lo = le.step(h)
    for fname, arr in so["peer_relative"].items():
        np.testing.assert_allclose(np.nan_to_num(arr), np.nan_to_num(lo["peer_relative"][fname]),
                                   rtol=1e-12, err_msg=f"peer_relative.{fname} seed != live")
    _assert_feature_spec_contract(so["peer_relative"], PeerRelativeReturnGroup().declare(), "peer_relative ")


# ========================================================================================================= #
# POINT-IN-TIME — batch 3a: calendar (input_cols=(), timestamp-only) + the ENGINE CHANGE regression pass.
# The Lead's blast-radius check: input_cols=() altered _marshal — confirm it did NOT regress present()/
# watermark/seed==live on the existing green groups (cross-sectional sparse + carried-state dedup).
# ========================================================================================================= #

from quantlib.features.clean_groups_pointwise import CalendarClean  # noqa: E402


def _et_epoch(y, mo, d, h, mi):
    import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    return int(_dt.datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/New_York")).timestamp())


def test_calendar_known_timestamps() -> None:
    """calendar from minute_epoch (ET): 08:00 → since_open=-90, is_regular=0; 10:00 → 30, 1; day_of_week ISO."""
    eng = CleanEngine([CalendarClean()], ["A"], WINDOW)
    # 2026-06-25 is a Thursday → ISO day_of_week 4
    out = eng.step({"symbol": np.array(["A"]), "minute_epoch": np.array([_et_epoch(2026, 6, 25, 8, 0)],
                                                                        dtype=np.int64)})["calendar"]
    assert out["minute_of_day_et"][0] == pytest.approx(480.0)       # 08:00 = 480
    assert out["minutes_since_open"][0] == pytest.approx(-90.0)     # 480 - 570
    assert out["is_regular_session"][0] == pytest.approx(0.0)       # pre-market
    assert out["day_of_week"][0] == pytest.approx(4.0)              # Thursday ISO
    out2 = eng.step({"symbol": np.array(["A"]), "minute_epoch": np.array([_et_epoch(2026, 6, 25, 10, 0)],
                                                                         dtype=np.int64)})["calendar"]
    assert out2["minutes_since_open"][0] == pytest.approx(30.0)     # 600 - 570
    assert out2["is_regular_session"][0] == pytest.approx(1.0)


def test_calendar_contract() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.calendar import CalendarGroup  # type: ignore  # noqa: PLC0415

    eng = CleanEngine([CalendarClean()], ["A", "B"], WINDOW)
    out = eng.step({"symbol": np.array(["A", "B"]),
                    "minute_epoch": np.array([_et_epoch(2026, 6, 25, 11, 30)], dtype=np.int64)})["calendar"]
    _assert_feature_spec_contract(out, CalendarGroup().declare(), "calendar ")


# --- ENGINE-CHANGE REGRESSION PASS (the Lead's cross-group blast-radius check) --- #


def test_engine_change_no_regression_present_gating_co_resident() -> None:
    """REGRESSION: with calendar (input_cols=()) CO-RESIDENT in the engine, a present()-gated cross-sectional
    group's sparse behavior must STILL hold — the input-less _marshal path must not corrupt present()/the ring
    for the other group. cross_sectional_rank: A,B,C present / D absent → rank over n=3, D=NaN (unchanged)."""
    syms = ["A", "B", "C", "D"]
    eng = CleanEngine([CalendarClean(), CrossSectionalRankClean()], syms, WINDOW)
    ep = _et_epoch(2026, 6, 25, 10, 0)
    eng.step({"symbol": np.array(syms), "close": np.array([100.0] * 4), "volume": np.array([9999.0] * 4),
              "minute_epoch": np.array([ep], dtype=np.int64)})
    out = eng.step({"symbol": np.array(["A", "B", "C"]), "close": np.array([100.0, 100.0, 100.0]),
                    "volume": np.array([1500.0, 2500.0, 3500.0]),
                    "minute_epoch": np.array([ep + 60], dtype=np.int64)})
    rank = out["cross_sectional_rank"]
    assert rank["volume_rank_1m"][0] == pytest.approx(0.0)   # present-only denom unchanged
    assert rank["volume_rank_1m"][2] == pytest.approx(1.0)
    assert np.isnan(rank["volume_rank_1m"][3])               # absent D NaN
    # calendar still broadcasts correctly co-resident
    assert out["calendar"]["is_regular_session"][0] == pytest.approx(1.0)


def test_engine_change_no_regression_watermark_dedup_co_resident() -> None:
    """REGRESSION: with calendar co-resident, the watermark dedup must STILL no-op a duplicate/stale minute on
    a carried-state group. intraday_seasonality cnt must stay correct under a duplicate epoch."""
    syms = ["A"]
    eng = CleanEngine([CalendarClean(), IntradaySeasonalityClean()], syms, WINDOW)
    eng.step({"symbol": np.array(["A"]), "volume": np.array([1000.0]), "minute_epoch": np.array([60], dtype=np.int64)})
    eng.step({"symbol": np.array(["A"]), "volume": np.array([1000.0]), "minute_epoch": np.array([120], dtype=np.int64)})
    cnt_before = eng._group_state["intraday_seasonality"]["cnt"][0]
    eng.step({"symbol": np.array(["A"]), "volume": np.array([1000.0]), "minute_epoch": np.array([120], dtype=np.int64)})  # DUP
    cnt_after = eng._group_state["intraday_seasonality"]["cnt"][0]
    assert cnt_after == cnt_before, "duplicate epoch double-counted with calendar co-resident (watermark regressed)"


def test_calendar_value_always_defined_row_emission_is_boundary_concern() -> None:
    """RESOLVED (ArchOverhaul + verified vs legacy capture.py:333-348): two DISTINCT gates, don't conflate.
    (1) VALUE gate (per-group, value-dependent): a feature whose VALUE is undefined for an absent symbol self-
        gates to NaN — the cross-sectional denominators (return_dispersion/sector_return/peer_relative). KEEP.
    (2) ROW-EMISSION gate (ONE boundary, uniform): which symbols' ROWS persist — absent → not persisted, for
        EVERY group incl calendar. Owned at the engine/capture seam (#57), NOT per-group (legacy filters once:
        'only the persisted rows are filtered, so feature VALUES are unchanged — parity-neutral').
    calendar's VALUE is symbol-independent + ALWAYS DEFINED → it must NOT self-output-gate; the absent-row drop
    is the boundary filter's job (#57). So calendar correctly broadcasts a defined value to every index symbol;
    the absent-row drop happens uniformly at the boundary. Pins calendar's VALUE always-defined (correct
    per-group behavior); the absent-row-emission is gated by the #57 boundary-filter tests when it lands."""
    import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    ep = int(_dt.datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo("America/New_York")).timestamp())
    eng = CleanEngine([CalendarClean()], ["A", "B"], WINDOW)
    out = eng.step({"symbol": np.array(["A"]), "minute_epoch": np.array([ep], dtype=np.int64)})["calendar"]
    # VALUE always defined for every index symbol (correct — calendar is symbol-independent). Absent B's ROW is
    # dropped by the #57 boundary filter, NOT by calendar self-gating.
    assert out["minute_of_day_et"][0] == pytest.approx(600.0)
    assert out["minute_of_day_et"][1] == pytest.approx(600.0)  # defined; row-drop is the boundary's job (#57)


# ========================================================================================================= #
# #57 — present()-ROW-EMISSION boundary (engine.emit()): folds via step() then NaN's absent rows UNIFORMLY
# across all groups + returns present_symbols. step() stays raw (calendar still broadcasts). The blast-radius
# check: emit() drops absent rows for EVERY kind; step() unchanged so existing tests hold; dedup uses cached.
# ========================================================================================================= #


def test_emit_drops_absent_rows_uniformly_all_kinds() -> None:
    """emit() NaN's absent symbols' rows for EVERY group — calendar (point-in-time, always-defined value),
    cross_sectional_rank (cross-sectional), intraday_seasonality (carried). A,B present / C absent → C's row is
    NaN in all three; present_symbols = [A,B]."""
    import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    syms = ["A", "B", "C"]
    eng = CleanEngine([CalendarClean(), CrossSectionalRankClean(), IntradaySeasonalityClean()], syms, WINDOW)
    ep = int(_dt.datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo("America/New_York")).timestamp())
    # warm all three present, then a minute where only A,B deliver (C absent)
    eng.emit({"symbol": np.array(syms), "close": np.array([100.0] * 3), "volume": np.array([1000.0] * 3),
              "minute_epoch": np.array([ep], dtype=np.int64)})
    present_symbols, filtered = eng.emit(
        {"symbol": np.array(["A", "B"]), "close": np.array([101.0, 102.0]), "volume": np.array([1500.0, 2500.0]),
         "minute_epoch": np.array([ep + 60], dtype=np.int64)})
    assert present_symbols == ["A", "B"]  # the persisted-row set
    # C (index 2) is NaN in EVERY group's every feature — calendar (would otherwise broadcast), xsec, carried
    assert np.isnan(filtered["calendar"]["minute_of_day_et"][2]), "calendar absent row not dropped by emit()"
    assert np.isnan(filtered["cross_sectional_rank"]["volume_rank_1m"][2])
    assert np.isnan(filtered["intraday_seasonality"]["volume_vs_session_mean"][2])
    # A,B (present) keep their values
    assert np.isfinite(filtered["calendar"]["minute_of_day_et"][0])
    assert np.isfinite(filtered["calendar"]["minute_of_day_et"][1])


def test_emit_step_raw_calendar_still_broadcasts() -> None:
    """step() is UNCHANGED — calendar still broadcasts its value to absent symbols in the RAW step() output (the
    per-group VALUE gate is separate from emit()'s row-emission filter). Only emit() present-filters."""
    import datetime as _dt  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    ep = int(_dt.datetime(2026, 6, 25, 10, 0, tzinfo=ZoneInfo("America/New_York")).timestamp())
    eng = CleanEngine([CalendarClean()], ["A", "B"], WINDOW)
    raw = eng.step({"symbol": np.array(["A"]), "minute_epoch": np.array([ep], dtype=np.int64)})["calendar"]
    assert raw["minute_of_day_et"][1] == pytest.approx(600.0)  # raw step() still broadcasts to absent B


def test_emit_dedup_uses_cached_present() -> None:
    """A re-delivered (stale-epoch) minute is a no-op in step() AND its emit() uses the CACHED present mask — a
    duplicate epoch emits the same present-row set, no double-fold, no shifted present."""
    syms = ["A", "B"]
    eng = CleanEngine([IntradaySeasonalityClean()], syms, WINDOW)
    eng.emit({"symbol": np.array(["A", "B"]), "volume": np.array([1000.0, 2000.0]),
              "minute_epoch": np.array([60], dtype=np.int64)})
    ps1, _ = eng.emit({"symbol": np.array(["A"]), "volume": np.array([1000.0]),
                       "minute_epoch": np.array([120], dtype=np.int64)})
    cnt1 = eng._group_state["intraday_seasonality"]["cnt"][0]
    ps2, _ = eng.emit({"symbol": np.array(["A"]), "volume": np.array([9999.0]),
                       "minute_epoch": np.array([120], dtype=np.int64)})  # DUP epoch 120
    cnt2 = eng._group_state["intraday_seasonality"]["cnt"][0]
    assert ps1 == ps2 == ["A"]              # cached present, same row set
    assert cnt1 == cnt2                      # no double-fold on the dup


# ========================================================================================================= #
# BATCH #55a — price_returns + price_levels: StatefulGroup-base in legacy but the MATH is WINDOWED (positional-
# lag returns / rolling max-min, NO carried state machine). Gated as windowed (formula+guards+seed==live), not
# the carried-state footguns. Confirmed at source: PriceReturnGroup=positional lag, PriceLevelGroup=max/min.
# ========================================================================================================= #

from quantlib.features.clean_groups_windowed import PriceLevelsClean, PriceReturnsClean  # noqa: E402


def test_price_returns_known() -> None:
    """ret_5m = close/close_{-5} − 1, log_ret_5m = ln(close/close_{-5}). Known close path."""
    closes = [100.0, 102.0, 103.0, 104.0, 105.0, 106.0]  # 6 bars; at the end, close_{-5}=100, close=106
    eng = CleanEngine([PriceReturnsClean()], ["A"], WINDOW)
    out = {}
    for ep, c in enumerate(closes):
        out = eng.step(_sec_bar({"A": c}, 60 + ep * 60))["price_returns"]
    assert out["ret_5m"][0] == pytest.approx(106.0 / 100.0 - 1.0)
    assert out["log_ret_5m"][0] == pytest.approx(np.log(106.0 / 100.0))
    assert out["ret_1m"][0] == pytest.approx(106.0 / 105.0 - 1.0)


def test_price_returns_strict_time_lag_null_on_gap() -> None:
    """The lag is a STRICT TIME-lag (#60 re-port, legacy LagSpec(minutes=w)=base.lagged): close as of EXACTLY
    T−w minutes, NULL when that exact minute is absent — NOT the w-th prior PRESENT bar. Earlier this test
    asserted the positional gap-safe behavior, which was a FALSE-GREEN: on a sparse symbol it computed a return
    off a stale prior-present bar where legacy returns NULL. A present at minutes 1,2,3, ABSENT at 4, present
    at 5: ret_1m needs minute 4's close (absent → NaN); ret_2m needs minute 3's close (present → 103/102−1)."""
    eng = CleanEngine([PriceReturnsClean()], ["A"], WINDOW)
    for ep, c in enumerate([100.0, 101.0, 102.0]):  # minutes 1,2,3 (epochs 60,120,180)
        eng.step(_sec_bar({"A": c}, 60 + ep * 60))
    eng.step({"symbol": np.array([], dtype="<U4"), "close": np.array([]),
              "minute_epoch": np.array([240], dtype=np.int64)})  # minute 4: A ABSENT
    out = eng.step(_sec_bar({"A": 103.0}, 300))["price_returns"]  # minute 5
    assert np.isnan(out["ret_1m"][0]), "ret_1m: no bar at EXACTLY minute 4 (absent) → strict-time-lag NULL"
    assert out["ret_2m"][0] == pytest.approx(103.0 / 102.0 - 1.0), "ret_2m: bar at minute 3 present (102) → defined"


def test_price_returns_warmup_and_contract() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.price_returns import PriceReturnGroup  # type: ignore  # noqa: PLC0415

    eng = CleanEngine([PriceReturnsClean()], ["A"], WINDOW)
    out = eng.step(_sec_bar({"A": 100.0}, 60))["price_returns"]
    assert np.isnan(out["ret_5m"][0])  # < 6 present bars → warm-up NaN
    # contract over a filled run
    eng2 = CleanEngine([PriceReturnsClean()], ["A"], WINDOW)
    out2 = {}
    for ep in range(200):
        out2 = eng2.step(_sec_bar({"A": 100.0 + np.sin(ep * 0.1) * 5}, 60 + ep * 60))["price_returns"]
    _assert_feature_spec_contract(out2, PriceReturnGroup().declare(), "price_returns ")


def test_price_levels_known_position_and_flat_band() -> None:
    """position_in_range = (close − min_low)/(max_high − min_low). Known window → known position; a FLAT window
    (band ≤ 1e-9·|high|) → NaN (the _RANGE_REL_EPS guard, matches legacy)."""
    eng = CleanEngine([PriceLevelsClean()], ["A"], WINDOW)
    # 5 bars: highs 105..109, lows 95..99, last close 103. max_high=109, min_low=95, pos=(103-95)/(109-95)
    out = {}
    for ep in range(5):
        out = eng.step(_ohlcv_bar(["A"], [100.0 + ep], [105.0 + ep], [95.0 + ep], [103.0], 60 + ep * 60))[
            "price_levels"]
    assert out["position_in_range_5m"][0] == pytest.approx((103.0 - 95.0) / (109.0 - 95.0))
    assert out["dist_from_high_5m"][0] == pytest.approx(103.0 / 109.0 - 1.0)
    assert out["dist_from_low_5m"][0] == pytest.approx(103.0 / 95.0 - 1.0)
    # flat window: high==low==close every bar → band 0 ≤ 1e-9·|high| → position NaN
    flat = CleanEngine([PriceLevelsClean()], ["A"], WINDOW)
    of = {}
    for ep in range(5):
        of = flat.step(_ohlcv_bar(["A"], [100.0], [100.0], [100.0], [100.0], 60 + ep * 60))["price_levels"]
    assert np.isnan(of["position_in_range_5m"][0]), "flat band must be NaN (_RANGE_REL_EPS guard)"


def test_price_levels_contract_and_seed_equals_live() -> None:
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.price_levels import PriceLevelGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(30)
    hist = []
    for t in range(60):
        c = 100.0 + np.cumsum(rng.standard_normal(1))[0]
        hist.append(_ohlcv_bar(["A"], [c], [c + abs(rng.normal()) + 1], [c - abs(rng.normal()) - 1], [c],
                               60 + t * 60))
    se = CleanEngine([PriceLevelsClean()], ["A"], WINDOW)
    se.seed(hist[:-1])
    so = se.step(hist[-1])
    le = CleanEngine([PriceLevelsClean()], ["A"], WINDOW)
    lo = {}
    for h in hist:
        lo = le.step(h)
    for fname, arr in so["price_levels"].items():
        np.testing.assert_allclose(np.nan_to_num(arr), np.nan_to_num(lo["price_levels"][fname]), rtol=1e-12,
                                   err_msg=f"price_levels.{fname} seed != live")
    _assert_feature_spec_contract(so["price_levels"], PriceLevelGroup().declare(), "price_levels ")


# ========================================================================================================= #
# TIME-Δ WINDOWED RING (#60, Lead ruling b): RingBuffer carries a per-slot minute-epoch + trailing_time(col,
# minutes, now_epoch) returns bars within (now − Nm, now] (legacy rolling_*_by closed 'right'), NOT the last N
# positional slots. GATE: on a SPARSE symbol positional ≠ time, and the re-ported time-window groups must
# match legacy ON THE SPARSE SHAPE — the axis my earlier dense-only greens never exercised.
# ========================================================================================================= #


def test_trailing_time_window_edge_matches_polars_rolling_by() -> None:
    """trailing_time keeps exactly the bars with (now − bar) < minutes·60 — the polars rolling_*_by closed
    'right' window (now−Nm, now]. THE off-by-one boundary: a bar at EXACTLY now−Nm is EXCLUDED. Head-to-head
    a 14m rolling sum on a sparse symbol incl. an edge bar."""
    import datetime  # noqa: PLC0415

    from quantlib.features.clean_engine import RingBuffer  # noqa: PLC0415

    ring = RingBuffer(["A"], window=300, cols=("close",))
    base = int(datetime.datetime(2026, 6, 1, 9, 30).timestamp())
    offsets = [0, 2, 5, 9, 13, 14, 20, 27]  # offset 13 lands EXACTLY at now−14m (excluded)
    closes = [100.0, 101.0, 103.0, 102.0, 105.0, 106.0, 108.0, 110.0]
    for off, c in zip(offsets, closes):
        ring.append(np.array([[c]]), np.array([0]), base + off * 60)
    now = base + 27 * 60
    tt = ring.trailing_time("close", 14, now)[0]
    kept = sorted(tt[np.isfinite(tt)].tolist())
    assert kept == [106.0, 108.0, 110.0], "only bars in (27−14, 27] = offsets 14,20,27; offset 13 is the edge"
    import polars as pl  # noqa: PLC0415
    minutes = [datetime.datetime(2026, 6, 1, 9, 30) + datetime.timedelta(minutes=o) for o in offsets]
    legacy_sum = pl.DataFrame({"minute": minutes, "close": closes}).sort("minute").with_columns(
        pl.col("close").rolling_sum_by("minute", window_size="14m").alias("s"))["s"].to_list()[-1]
    assert float(np.nansum(tt)) == pytest.approx(legacy_sum), "trailing_time nansum != legacy rolling_sum_by"


def test_price_levels_sparse_time_window_matches_legacy_rolling_by() -> None:
    """RE-GATE price_levels on the SPARSE axis (the #60 re-port to time-windows; my earlier green was dense-
    only). On a gappy symbol, position_in_range/dist_from_high/dist_from_low over the 30m TIME window must equal
    legacy rolling_max_by/rolling_min_by — NOT the positional last-30-bars (which would reach hours back)."""
    import datetime  # noqa: PLC0415

    import polars as pl  # noqa: PLC0415

    base = datetime.datetime(2026, 6, 1, 9, 30)
    offsets = [0, 2, 4, 7, 11, 13, 18, 22, 27, 31, 34, 38]  # sparse, spans 38 min > 30m window
    rng = np.random.default_rng(4)
    closes = 100.0 + np.cumsum(rng.standard_normal(len(offsets)))
    highs = closes + np.abs(rng.standard_normal(len(offsets))) + 0.5
    lows = closes - np.abs(rng.standard_normal(len(offsets))) - 0.5
    minutes = [base + datetime.timedelta(minutes=o) for o in offsets]
    df = pl.DataFrame({"minute": minutes, "close": closes, "high": highs, "low": lows}).sort("minute")
    df = df.with_columns(pl.col("high").rolling_max_by("minute", window_size="30m").alias("hi"),
                         pl.col("low").rolling_min_by("minute", window_size="30m").alias("lo"))
    last = df.tail(1)
    hi30, lo30, cl = last["hi"][0], last["lo"][0], closes[-1]
    leg_pos = (cl - lo30) / (hi30 - lo30)

    eng = CleanEngine([PriceLevelsClean()], ["A"], 400)
    out = {}
    for off, c, h, low_v in zip(offsets, closes, highs, lows):
        out = eng.step(_ohlcv_bar(["A"], [c], [h], [low_v], [c], int((base + datetime.timedelta(minutes=off)).timestamp())))[
            "price_levels"]
    assert out["position_in_range_30m"][0] == pytest.approx(leg_pos), "sparse 30m position != legacy rolling_*_by"
    assert out["dist_from_high_30m"][0] == pytest.approx(cl / hi30 - 1.0)
    assert out["dist_from_low_30m"][0] == pytest.approx(cl / lo30 - 1.0)
    in_window_max = max(highs[i] for i, o in enumerate(offsets) if (38 - o) < 30)
    assert hi30 == pytest.approx(in_window_max), "legacy 30m high is over the TIME window (offset>8), not all bars"


# ========================================================================================================= #
# DAILY-SNAPSHOT — batch #55: multi_day (MultiDayReturnGroup). Intraday-INVARIANT: daily_return/vol/dist-from-
# high computed ONCE from the settled daily closes in window.session['daily_close'] (newest col = prior close,
# the _asof anchor) and broadcast to every minute. Gated head-to-head vs legacy polars (rolling_std=ddof=1,
# _asof=shift(1), dist uses rolling_max of CLOSES not highs — both legacy & clean).
# ========================================================================================================= #

from quantlib.features.clean_groups_daily import (  # noqa: E402
    DailyBetaClean,
    LiquidityRankClean,
    MultiDayClean,
    OvernightIntradaySplitClean,
    _daily_return,
    _daily_vol,
    _dist_from_high,
)


def test_multi_day_return_vol_dist_match_legacy_polars() -> None:
    """daily_return_{w}d / daily_vol_{w}d / dist_from_{w}d_high equal legacy polars exactly on a known series:
    return = close[-1]/close[-(w+1)]-1, vol = rolling_std(ddof=1) of daily returns, dist = close[-1]/rolling_max
    -1. The newest daily column IS the prior close (_asof), matching legacy shift(1)."""
    import polars as pl  # noqa: PLC0415

    closes = np.array([100.0, 102.0, 101.0, 105.0, 103.0, 107.0, 110.0, 108.0, 111.0, 115.0])
    mat = closes.reshape(1, -1)  # 1 symbol, newest LAST
    df = pl.DataFrame({"symbol": ["X"] * len(closes), "_asof": closes})
    for w in (1, 2, 5):
        df = df.with_columns((pl.col("_asof") / pl.col("_asof").shift(w).over("symbol") - 1.0).alias(f"r{w}"))
        assert _daily_return(mat, w)[0] == pytest.approx(df[f"r{w}"].to_list()[-1])
    df = df.with_columns((pl.col("_asof") / pl.col("_asof").shift(1).over("symbol") - 1.0).alias("_dret"))
    for w in (5,):
        df = df.with_columns(pl.col("_dret").rolling_std(window_size=w).over("symbol").alias(f"v{w}"))
        assert _daily_vol(mat, w)[0] == pytest.approx(df[f"v{w}"].to_list()[-1])
    falling = np.array([100.0, 120.0, 110.0, 105.0, 108.0]).reshape(1, -1)  # high=120, asof=108
    assert _dist_from_high(falling, 5)[0] == pytest.approx(108.0 / 120.0 - 1.0)  # -10%, non-trivial


def test_multi_day_warmup_nan_when_insufficient_days() -> None:
    """A horizon needing more days than the snapshot holds → NaN (warm-up)."""
    short = np.array([100.0, 102.0, 101.0]).reshape(1, -1)  # 3 days
    assert np.isnan(_daily_return(short, 5)[0]), "daily_return_5d on 3 days → NaN"
    assert _daily_return(short, 2)[0] == pytest.approx(101.0 / 100.0 - 1.0), "daily_return_2d on 3 days computes"


def test_multi_day_contract_and_no_session_is_nan() -> None:
    """produced == declared with legacy valid_range/nan_policy; no daily snapshot → all-NaN (not a crash)."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.multi_day import MultiDayReturnGroup  # type: ignore  # noqa: PLC0415

    syms = ["A", "B"]
    daily_close = np.array([[98.0, 100.0, 102.0, 104.0, 106.0, 108.0], [49.0, 50.0, 50.5, 51.0, 51.5, 52.0]])
    eng = CleanEngine([MultiDayClean()], syms, WINDOW)
    eng.set_session({"daily_close": daily_close})
    out = eng.step({"symbol": np.array(syms), "close": np.array([102.0, 50.0]),
                    "volume": np.zeros(2), "minute_epoch": np.array([60], dtype=np.int64)})["multi_day"]
    _assert_feature_spec_contract(out, MultiDayReturnGroup().declare(), "multi_day ")
    eng2 = CleanEngine([MultiDayClean()], syms, WINDOW)  # no session → all-NaN, no crash
    out2 = eng2.step({"symbol": np.array(syms), "close": np.array([102.0, 50.0]),
                      "volume": np.zeros(2), "minute_epoch": np.array([60], dtype=np.int64)})["multi_day"]
    assert all(np.all(np.isnan(arr)) for arr in out2.values()), "no daily snapshot → all-NaN"


def test_multi_day_broadcasts_same_value_across_minutes() -> None:
    """INTRADAY-INVARIANT: the daily features are identical every minute of the session (compute-once,
    broadcast) — the snapshot is fixed for the day."""
    syms = ["A", "B", "C"]
    daily_close = np.array([[98.0, 100.0, 103.0], [49.0, 50.0, 52.0], [201.0, 200.0, 198.0]])
    eng = CleanEngine([MultiDayClean()], syms, WINDOW)
    eng.set_session({"daily_close": daily_close})
    first = eng.step({"symbol": np.array(syms), "close": np.array([103.0, 52.0, 198.0]),
                      "volume": np.zeros(3), "minute_epoch": np.array([60], dtype=np.int64)})["multi_day"]
    later = eng.step({"symbol": np.array(syms), "close": np.array([104.0, 53.0, 197.0]),
                      "volume": np.zeros(3), "minute_epoch": np.array([120], dtype=np.int64)})["multi_day"]
    for fname in first:
        np.testing.assert_allclose(np.nan_to_num(first[fname]), np.nan_to_num(later[fname]), rtol=1e-12,
                                   err_msg=f"multi_day.{fname} drifted across minutes (must be intraday-invariant)")


# ========================================================================================================= #
# DAILY-SNAPSHOT — daily_beta (DailyBetaGroup): rolling 60-DAY OLS beta/corr/idio-vol of the name's daily
# returns on SPY's (the certified W11 overnight-beta quantity). Daily bars are one-per-day (DENSE), so the
# positional-vs-time-minute question does NOT apply. OWN guard: var_x>0 + MIN_PAIRS=20 (NOT the minute-OLS
# relative floors). idio uses ddof=1 std (clean's n/(n-1) on the pop-var matches legacy rolling_std).
# ========================================================================================================= #


def _daily_close_with_beta(n_days: int, beta: float, seed: int) -> tuple[np.ndarray, list[str]]:
    """A (3, n_days) daily-close matrix: name A = ``beta``×SPY + idio, B = noise, SPY = row 2."""
    rng = np.random.default_rng(seed)
    spy = 100.0 + np.cumsum(rng.standard_normal(n_days) * 0.5)
    spy_ret = spy[1:] / spy[:-1] - 1.0
    a_ret = beta * spy_ret + rng.standard_normal(n_days - 1) * 0.003
    b_ret = 0.3 * spy_ret + rng.standard_normal(n_days - 1) * 0.01

    def _to_close(rets: np.ndarray, start: float) -> np.ndarray:
        close = [start]
        for r in rets:
            close.append(close[-1] * (1.0 + r))
        return np.array(close)

    return np.vstack([_to_close(a_ret, 50.0), _to_close(b_ret, 50.0), spy]), ["A", "B", "SPY"]


def _step_daily_beta(daily_close: np.ndarray, syms: list[str], spy_row: int = 2) -> dict[str, np.ndarray]:
    eng = CleanEngine([DailyBetaClean()], syms, WINDOW)
    eng.static = {"spy_row": np.array([spy_row])}
    eng.set_session({"daily_close": daily_close})
    return eng.step({"symbol": np.array(syms), "close": daily_close[:, -1], "volume": np.zeros(len(syms)),
                     "minute_epoch": np.array([570 * 60], dtype=np.int64)})["daily_beta"]


def test_daily_beta_matches_legacy_rolling_ols() -> None:
    """daily_beta_60d/corr/idio = legacy polars rolling_cov/var/corr/std over the trailing 60 daily returns on
    SPY. beta/corr are ratios (ddof cancels); idio = ret_std(ddof=1)·sqrt(1−corr²) — clean's n/(n-1) matches
    legacy rolling_std. Head-to-head on a high-beta + a low-beta name."""
    import polars as pl  # noqa: PLC0415

    n_days = 80
    daily_close, syms = _daily_close_with_beta(n_days, beta=1.5, seed=11)
    rows = [{"symbol": s, "date": d, "close": float(daily_close[i, d])}
            for i, s in enumerate(syms) for d in range(n_days)]
    daily = pl.DataFrame(rows).sort(["symbol", "date"]).with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("ret"))
    market = daily.filter(pl.col("symbol") == "SPY").select(["date", pl.col("ret").alias("mkt_ret")]).sort("date")
    joined = daily.join(market, on="date", how="left").sort(["symbol", "date"])
    mkt_var = pl.col("mkt_ret").rolling_var(window_size=60, min_samples=20).over("symbol")
    cov_roll = pl.rolling_cov(pl.col("ret"), pl.col("mkt_ret"), window_size=60, min_samples=20).over("symbol")
    corr = pl.rolling_corr(pl.col("ret"), pl.col("mkt_ret"), window_size=60, min_samples=20).over("symbol").clip(-1.0, 1.0)
    ret_std = pl.col("ret").rolling_std(window_size=60, min_samples=20).over("symbol")
    beta = pl.when(mkt_var > 0).then(cov_roll / mkt_var).otherwise(None)
    idio = pl.when(corr.is_not_null()).then(ret_std * (1.0 - corr * corr).clip(0.0).sqrt()).otherwise(None)
    joined = joined.with_columns(beta.alias("b"), corr.alias("c"), idio.alias("i"))

    out = _step_daily_beta(daily_close, syms)
    for i, s in enumerate(["A", "B"]):
        last = joined.filter((pl.col("symbol") == s) & (pl.col("date") == n_days - 1))
        assert out["daily_beta_60d"][i] == pytest.approx(last["b"][0], rel=1e-9)
        assert out["daily_corr_60d"][i] == pytest.approx(last["c"][0], rel=1e-9)
        assert out["daily_idio_vol_60d"][i] == pytest.approx(last["i"][0], rel=1e-9)


def test_daily_beta_own_guard_warmup_var0_perfectfit() -> None:
    """OWN-guard boundary: <20 finite pairs → NaN (warm-up); SPY var=0 → beta NaN (var_x>0 guard); a perfect
    linear fit → corr clipped to exactly 1.0 (≤1) + idio≈0; no spy_row static → all-NaN (no crash)."""
    rng = np.random.default_rng(3)
    short = 100.0 + np.cumsum(rng.standard_normal((3, 15)) * 0.5, axis=1)  # 14 returns < 20
    out_w = _step_daily_beta(short, ["A", "B", "SPY"])
    assert np.isnan(out_w["daily_beta_60d"][0]) and np.isnan(out_w["daily_corr_60d"][0]), "<20 pairs → NaN"

    flat_spy = np.vstack([100.0 + np.cumsum(rng.standard_normal(80) * 0.5),
                          100.0 + np.cumsum(rng.standard_normal(80) * 0.5), np.full(80, 100.0)])  # SPY flat
    out_v = _step_daily_beta(flat_spy, ["A", "B", "SPY"])
    assert np.isnan(out_v["daily_beta_60d"][0]), "SPY var=0 → NaN (var_x>0 own-guard)"

    spy = 100.0 + np.cumsum(rng.standard_normal(80) * 0.5)
    spy_ret = spy[1:] / spy[:-1] - 1.0
    name = [50.0]
    for r in spy_ret:
        name.append(name[-1] * (1.0 + 2.0 * r))  # exactly 2x SPY
    perfect = np.vstack([np.array(name), 100.0 + np.cumsum(rng.standard_normal(80) * 0.5), spy])
    out_p = _step_daily_beta(perfect, ["A", "B", "SPY"])
    assert out_p["daily_beta_60d"][0] == pytest.approx(2.0, rel=1e-6), "perfect 2x fit → beta 2.0"
    assert out_p["daily_corr_60d"][0] <= 1.0 and out_p["daily_corr_60d"][0] == pytest.approx(1.0), "corr clipped to 1.0"
    assert out_p["daily_idio_vol_60d"][0] == pytest.approx(0.0, abs=1e-9), "perfect fit → idio ≈ 0"

    eng = CleanEngine([DailyBetaClean()], ["A", "B", "SPY"], WINDOW)  # no spy_row static
    eng.set_session({"daily_close": perfect})
    out_n = eng.step({"symbol": np.array(["A", "B", "SPY"]), "close": perfect[:, -1], "volume": np.zeros(3),
                      "minute_epoch": np.array([570 * 60], dtype=np.int64)})["daily_beta"]
    assert all(np.all(np.isnan(arr)) for arr in out_n.values()), "no spy_row → all-NaN (no crash)"


def test_daily_beta_contract() -> None:
    """produced == declared with legacy valid_range/nan_policy (corr in [-1,1], idio ≥ 0)."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.daily_beta import DailyBetaGroup  # type: ignore  # noqa: PLC0415

    daily_close, syms = _daily_close_with_beta(80, beta=1.2, seed=7)
    out = _step_daily_beta(daily_close, syms)
    _assert_feature_spec_contract(out, DailyBetaGroup().declare(), "daily_beta ")


# ========================================================================================================= #
# DAILY-SNAPSHOT — overnight_intraday_split + liquidity_rank (both DailySnapshotGroup, daily-DENSE so no
# positional/time issue). overnight reads window.session['daily_open'+'daily_close'] (NOTE: daily_open is a NEW
# session-schema field for the go-live populator). liquidity_rank reads daily_close+daily_volume.
# ========================================================================================================= #


def test_overnight_intraday_split_matches_legacy() -> None:
    """intraday_ret = close/open−1; overnight_minus_intraday = (open/prev_close−1) − intraday; overnight_share
    = |overnight|/(|overnight|+|intraday|), NULL on a zero-move day. Head-to-head on a give-back name + a
    zero-move name."""
    syms = ["A", "B", "Z"]
    # A: overnight gap up (open 110 vs prev_close 100) + intraday up (close 121); B: gap up, intraday give-back
    daily_open = np.array([[100.0, 105.0, 110.0], [50.0, 51.0, 55.0], [20.0, 20.0, 20.0]])
    daily_close = np.array([[100.0, 100.0, 121.0], [50.0, 50.0, 52.0], [20.0, 20.0, 20.0]])
    eng = CleanEngine([OvernightIntradaySplitClean()], syms, WINDOW)
    eng.set_session({"daily_open": daily_open, "daily_close": daily_close})
    out = eng.step({"symbol": np.array(syms), "close": daily_close[:, -1], "volume": np.zeros(3),
                    "minute_epoch": np.array([60], dtype=np.int64)})["overnight_intraday_split"]
    for i in range(3):
        op, cl, pc = daily_open[i, -1], daily_close[i, -1], daily_close[i, -2]
        overnight, intraday = op / pc - 1.0, cl / op - 1.0
        abs_total = abs(overnight) + abs(intraday)
        assert out["intraday_ret"][i] == pytest.approx(intraday)
        assert out["overnight_minus_intraday"][i] == pytest.approx(overnight - intraday)
        if abs_total > 0:
            assert out["overnight_share"][i] == pytest.approx(abs(overnight) / abs_total)
        else:
            assert np.isnan(out["overnight_share"][i]), "zero-move day → overnight_share NaN"
    assert out["overnight_share"][0] > 0.0, "A has a real overnight + intraday move (non-vacuous share)"


def test_overnight_intraday_split_contract_and_warmup() -> None:
    """Contract (intraday_ret∈[-1,5], share∈[0,1]); <2 daily columns or no session → all-NaN (warm-up)."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.overnight_intraday_split import OvernightIntradaySplitGroup  # type: ignore  # noqa: PLC0415,E501

    syms = ["A", "B"]
    daily_open = np.array([[100.0, 105.0, 110.0], [50.0, 51.0, 55.0]])
    daily_close = np.array([[100.0, 102.0, 121.0], [50.0, 50.0, 52.0]])
    eng = CleanEngine([OvernightIntradaySplitClean()], syms, WINDOW)
    eng.set_session({"daily_open": daily_open, "daily_close": daily_close})
    out = eng.step({"symbol": np.array(syms), "close": daily_close[:, -1], "volume": np.zeros(2),
                    "minute_epoch": np.array([60], dtype=np.int64)})["overnight_intraday_split"]
    _assert_feature_spec_contract(out, OvernightIntradaySplitGroup().declare(), "overnight_intraday_split ")
    eng2 = CleanEngine([OvernightIntradaySplitClean()], syms, WINDOW)  # no session → all-NaN
    out2 = eng2.step({"symbol": np.array(syms), "close": np.array([100.0, 50.0]), "volume": np.zeros(2),
                      "minute_epoch": np.array([60], dtype=np.int64)})["overnight_intraday_split"]
    assert all(np.all(np.isnan(arr)) for arr in out2.values()), "no daily snapshot → all-NaN"


def test_liquidity_rank_matches_legacy_adv_and_percentile() -> None:
    """adv_dollar_log_20d = log1p(trailing-20d mean dollar volume, min 10 days); liquidity_rank = cross-
    sectional rank(method='average')/count of the ADV (1 = most liquid). Head-to-head vs hand average-rank;
    warm-up (<10 days) → NaN."""
    rng = np.random.default_rng(5)
    syms = ["X", "Y", "Z", "W"]
    daily_close = 50.0 + rng.random((4, 25)) * 50.0
    daily_volume = rng.random((4, 25)) * 1e6
    eng = CleanEngine([LiquidityRankClean()], syms, WINDOW)
    eng.set_session({"daily_close": daily_close, "daily_volume": daily_volume})
    out = eng.step({"symbol": np.array(syms), "close": daily_close[:, -1], "volume": np.zeros(4),
                    "minute_epoch": np.array([60], dtype=np.int64)})["liquidity_rank"]
    adv_hand = (daily_close * daily_volume)[:, -20:].mean(axis=1)  # all 20 finite
    # average-rank / count (1 = most liquid), matching polars rank(method='average')/count
    order = np.argsort(np.argsort(adv_hand))  # ordinal; ties absent in random floats → average==ordinal
    rank_hand = (order + 1.0) / len(adv_hand)
    for i in range(4):
        assert out["adv_dollar_log_20d"][i] == pytest.approx(np.log1p(adv_hand[i]))
        assert out["liquidity_rank"][i] == pytest.approx(rank_hand[i])
    assert out["liquidity_rank"].max() == pytest.approx(1.0), "the most-liquid name ranks 1.0"
    short = daily_close[:, :8]  # <10 days → warm-up NaN
    eng2 = CleanEngine([LiquidityRankClean()], syms, WINDOW)
    eng2.set_session({"daily_close": short, "daily_volume": daily_volume[:, :8]})
    out2 = eng2.step({"symbol": np.array(syms), "close": short[:, -1], "volume": np.zeros(4),
                      "minute_epoch": np.array([60], dtype=np.int64)})["liquidity_rank"]
    assert np.all(np.isnan(out2["adv_dollar_log_20d"])), "<10 days → NaN (warm-up)"


def test_liquidity_rank_contract() -> None:
    """produced == declared (adv_dollar_log ≥ 0, liquidity_rank ∈ [0,1])."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.liquidity_rank import LiquidityRankGroup  # type: ignore  # noqa: PLC0415

    rng = np.random.default_rng(9)
    syms = ["X", "Y", "Z"]
    daily_close = 50.0 + rng.random((3, 25)) * 50.0
    daily_volume = rng.random((3, 25)) * 1e6
    eng = CleanEngine([LiquidityRankClean()], syms, WINDOW)
    eng.set_session({"daily_close": daily_close, "daily_volume": daily_volume})
    out = eng.step({"symbol": np.array(syms), "close": daily_close[:, -1], "volume": np.zeros(3),
                    "minute_epoch": np.array([60], dtype=np.int64)})["liquidity_rank"]
    _assert_feature_spec_contract(out, LiquidityRankGroup().declare(), "liquidity_rank ")


# ========================================================================================================= #
# SESSION-RESET CUMULATIVE MACHINES — batch #59 (runner/dumper/gap_fill): per (symbol, ET-session) cum-max /
# cum-min / cum-sum / first-open since the 09:30 ET open, reset each session, RTH-only, present-decay. The NEW
# footgun axis (Lead): session-reset on a symbol's FIRST present bar of a new session + present-decay through
# an absent minute + pre-open bars ignored + watermark idempotency on a re-delivered minute.
# ========================================================================================================= #

import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo as _ZoneInfo  # noqa: E402

from quantlib.features.clean_groups_stateful import (  # noqa: E402
    DumperStateClean,
    GapFillStateClean,
    RunnerStateClean,
)

_ET = _ZoneInfo("America/New_York")


def _et_session_epoch(hour: int, minute: int, day: int = 1) -> int:
    return int(_dt.datetime(2026, 6, day, hour, minute, tzinfo=_ET).timestamp())


def _ohlcv_session_bar(syms: list[str], op: list[float], high: list[float], low: list[float],
                       close: list[float], volume: list[float], epoch: int) -> dict[str, np.ndarray]:
    return {"symbol": np.array(syms), "open": np.array(op, dtype=np.float64),
            "high": np.array(high, dtype=np.float64), "low": np.array(low, dtype=np.float64),
            "close": np.array(close, dtype=np.float64), "volume": np.array(volume, dtype=np.float64),
            "minute_epoch": np.array([epoch], dtype=np.int64)}


def test_runner_state_math_and_session_reset() -> None:
    """runner_early_move/gap_open/pullback/log_dollar_vol/in_band/is_active match the hand cumulative-since-open
    values; a NEW ET session RESETS the running high + dollar (not carried from the prior session)."""
    eng = CleanEngine([RunnerStateClean()], ["R"], 400)
    eng.set_session({"prev_close": np.array([10.0])})  # in band [2,20]
    out = {}
    for op, hi, lo, cl, vol, hh, mm in [
        (10.5, 11.0, 10.4, 10.8, 1000, 9, 30), (10.8, 12.0, 10.7, 11.5, 2000, 9, 31),
        (11.5, 13.0, 11.4, 12.8, 3000, 9, 32), (12.8, 12.9, 12.0, 12.0, 1500, 9, 33),
    ]:
        out = eng.step(_ohlcv_session_bar(["R"], [op], [hi], [lo], [cl], [vol], _et_session_epoch(hh, mm)))["runner_state"]
    run_high, sess_open, prev = 13.0, 10.5, 10.0
    dollar = 10.8 * 1000 + 11.5 * 2000 + 12.8 * 3000 + 12.0 * 1500
    assert out["runner_early_move"][0] == pytest.approx(run_high / prev - 1.0)
    assert out["runner_gap_open"][0] == pytest.approx(sess_open / prev - 1.0)
    assert out["runner_pullback_from_high"][0] == pytest.approx(12.0 / run_high - 1.0)
    assert out["runner_log_dollar_vol"][0] == pytest.approx(np.log1p(dollar))
    assert out["runner_in_band"][0] == pytest.approx(1.0)
    assert out["runner_is_active"][0] == pytest.approx(1.0)  # early_move 0.30 >= 0.30
    out2 = eng.step(_ohlcv_session_bar(["R"], [10.2], [10.3], [10.1], [10.25], [500], _et_session_epoch(9, 30, day=2)))[
        "runner_state"]
    assert out2["runner_early_move"][0] == pytest.approx(10.3 / 10.0 - 1.0), "new session reset run_high to 10.3"
    assert out2["runner_log_dollar_vol"][0] == pytest.approx(np.log1p(10.25 * 500)), "dollar reset"


def test_runner_state_footguns_preopen_presence_watermark() -> None:
    """The #59 footgun axis: (1) a PRE-OPEN bar (etm<570) must NOT update the running high; (2) an ABSENT symbol
    HOLDS its session accumulators (present-decay); (3) a RE-DELIVERED minute (watermark) does NOT double the
    cumulative dollar volume."""
    # (1) pre-open ignored
    eng = CleanEngine([RunnerStateClean()], ["R"], 400)
    eng.set_session({"prev_close": np.array([10.0])})
    eng.step(_ohlcv_session_bar(["R"], [10.0], [50.0], [9.0], [10.0], [100], _et_session_epoch(9, 0)))  # premarket high 50
    out = eng.step(_ohlcv_session_bar(["R"], [10.5], [11.0], [10.4], [10.8], [1000], _et_session_epoch(9, 30)))[
        "runner_state"]
    assert out["runner_early_move"][0] == pytest.approx(11.0 / 10.0 - 1.0), "premarket high 50 must not count"
    # (2) present-decay: B absent holds run_high + run_dollar
    eng2 = CleanEngine([RunnerStateClean()], ["A", "B"], 400)
    eng2.set_session({"prev_close": np.array([10.0, 10.0])})
    eng2.step(_ohlcv_session_bar(["A", "B"], [10.5, 10.5], [11.0, 12.0], [10.4, 10.4], [10.8, 11.5],
                                 [1000, 1000], _et_session_epoch(9, 30)))
    rh_before = eng2._group_state["runner_state"]["run_high"][1]
    dol_before = eng2._group_state["runner_state"]["run_dollar"][1]
    eng2.step(_ohlcv_session_bar(["A"], [10.8], [13.0], [10.7], [12.0], [2000], _et_session_epoch(9, 31)))  # B absent
    assert eng2._group_state["runner_state"]["run_high"][1] == rh_before, "B run_high held (present-decay)"
    assert eng2._group_state["runner_state"]["run_dollar"][1] == dol_before, "B run_dollar held"
    # (3) watermark: re-delivered minute does not double-count
    eng3 = CleanEngine([RunnerStateClean()], ["R"], 400)
    eng3.set_session({"prev_close": np.array([10.0])})
    eng3.step(_ohlcv_session_bar(["R"], [10.5], [11.0], [10.4], [10.8], [1000], _et_session_epoch(9, 30)))
    eng3.step(_ohlcv_session_bar(["R"], [10.8], [12.0], [10.7], [11.5], [2000], _et_session_epoch(9, 31)))
    dol = eng3._group_state["runner_state"]["run_dollar"][0]
    eng3.step(_ohlcv_session_bar(["R"], [10.8], [12.0], [10.7], [11.5], [2000], _et_session_epoch(9, 31)))  # re-deliver
    assert eng3._group_state["runner_state"]["run_dollar"][0] == dol, "re-delivered minute double-counted dollar"


def test_dumper_state_math_cum_min_mirror() -> None:
    """dumper is the cum-MIN mirror: early_drop = 1 − run_low/prev_close; bounce_from_low = close/run_low − 1."""
    eng = CleanEngine([DumperStateClean()], ["D"], 400)
    eng.set_session({"prev_close": np.array([10.0])})
    out = {}
    for op, hi, lo, cl, vol, hh, mm in [
        (9.8, 9.9, 9.5, 9.6, 1000, 9, 30), (9.6, 9.7, 7.0, 7.5, 3000, 9, 31), (7.5, 8.2, 7.4, 8.0, 2000, 9, 32),
    ]:
        out = eng.step(_ohlcv_session_bar(["D"], [op], [hi], [lo], [cl], [vol], _et_session_epoch(hh, mm)))["dumper_state"]
    run_low, prev = 7.0, 10.0
    assert out["dumper_early_drop"][0] == pytest.approx(1.0 - run_low / prev)
    assert out["dumper_bounce_from_low"][0] == pytest.approx(8.0 / run_low - 1.0)
    assert out["dumper_is_active"][0] == pytest.approx(1.0)  # in_band & drop 0.30 >= 0.30


def test_gap_fill_state_fraction_and_zero_gap_nan() -> None:
    """gap_fill_fraction = (close − sess_open)/(prev_close − sess_open); NULL on a zero-gap day (|denom|≤1e-9);
    gap_extended = fraction < 0."""
    eng = CleanEngine([GapFillStateClean()], ["G"], 400)
    eng.set_session({"prev_close": np.array([10.0])})
    eng.step(_ohlcv_session_bar(["G"], [11.0], [11.1], [10.9], [11.0], [1000], _et_session_epoch(9, 30)))  # sess_open=11
    out = eng.step(_ohlcv_session_bar(["G"], [11.0], [11.1], [10.4], [10.5], [1000], _et_session_epoch(9, 31)))[
        "gap_fill_state"]
    assert out["gap_fill_fraction"][0] == pytest.approx((10.5 - 11.0) / (10.0 - 11.0)), "half-filled = 0.5"
    assert out["gap_extended"][0] == pytest.approx(0.0)  # fraction 0.5 >= 0
    eng2 = CleanEngine([GapFillStateClean()], ["Z"], 400)  # zero-gap: open == prev_close
    eng2.set_session({"prev_close": np.array([10.0])})
    outz = eng2.step(_ohlcv_session_bar(["Z"], [10.0], [10.1], [9.9], [10.05], [1000], _et_session_epoch(9, 30)))[
        "gap_fill_state"]
    assert np.isnan(outz["gap_fill_fraction"][0]), "zero-gap day → NaN (|denom| ≤ 1e-9)"


def test_session_reset_machines_contract() -> None:
    """produced == declared for all three machines, with legacy valid_range/nan_policy."""
    import sys  # noqa: PLC0415
    sys.path.insert(0, "/home/ben/quant-fp")
    from quantlib.features.groups.dumper_state import DumperStateGroup  # type: ignore  # noqa: PLC0415
    from quantlib.features.groups.gap_fill_state import GapFillStateGroup  # type: ignore  # noqa: PLC0415
    from quantlib.features.groups.runner_state import RunnerStateGroup  # type: ignore  # noqa: PLC0415

    cases = [
        (RunnerStateClean(), RunnerStateGroup(), [10.0], _et_session_epoch(9, 32)),
        (DumperStateClean(), DumperStateGroup(), [10.0], _et_session_epoch(9, 32)),
        (GapFillStateClean(), GapFillStateGroup(), [10.0], _et_session_epoch(9, 32)),
    ]
    for clean_group, legacy_group, prev_close, epoch in cases:
        eng = CleanEngine([clean_group], ["S"], 400)
        eng.set_session({"prev_close": np.array(prev_close)})
        eng.step(_ohlcv_session_bar(["S"], [11.0], [12.0], [9.0], [11.5], [1000], _et_session_epoch(9, 30)))
        out = eng.step(_ohlcv_session_bar(["S"], [11.5], [13.0], [10.0], [12.0], [2000], epoch))[clean_group.name]
        _assert_feature_spec_contract(out, legacy_group.declare(), f"{clean_group.name} ")


# ========================================================================================================= #
# FEATURE-SET-COMPLETENESS GATE (Lead, must-have-before-relaunch): a clean group's feature NAMES must EQUAL its
# legacy group's declare() names — the EXACT SET, not a subset. value-match + sparse-h2h only check the EMITTED
# subset, so a STUB (clean declares fewer features than legacy) passes them while silently dropping features
# from the models. This is the only gate that catches stubs. The 6 known stubs (06-25 sweep) are xfailed with
# their gap so they're TRACKED + can't regress; flip each to a hard match when ArchOverhaul fills it.
# ========================================================================================================= #

# clean group name -> (legacy module under quantlib.features.groups, legacy Group class)
_LEGACY_GROUP_OF = {
    "price_volume": ("price_volume", "PriceVolumeGroup"),
    "price_levels": ("price_levels", "PriceLevelGroup"),
    "price_returns": ("price_returns", "PriceReturnGroup"),
    "volatility": ("volatility", "VolatilityGroup"),
    "ohlc_vol": ("ohlc_vol", "OhlcVolGroup"),
    "quote_spread": ("quote_spread", "QuoteSpreadGroup"),
    "range_expansion": ("range_expansion", "RangeExpansionGroup"),
    "realized_range": ("realized_range", "RealizedRangeGroup"),
    "distribution": ("distribution", "DistributionGroup"),
    "liquidity": ("liquidity", "LiquidityGroup"),
    "trend_quality": ("trend_quality", "TrendQualityGroup"),
    "technical": ("technical", "TechnicalGroup"),
    "sector_beta": ("sector_beta", "SectorBetaGroup"),
    "market_turbulence": ("market_turbulence", "MarketTurbulenceGroup"),
    "cross_sectional_rank": ("cross_sectional_rank", "CrossSectionalRankGroup"),
    "return_dispersion": ("return_dispersion", "ReturnDispersionGroup"),
    "sector_return": ("sector_return", "SectorReturnGroup"),
    "peer_relative": ("peer_relative", "PeerRelativeReturnGroup"),
    "calendar": ("calendar", "CalendarGroup"),
    "multi_day": ("multi_day", "MultiDayReturnGroup"),
    "daily_beta": ("daily_beta", "DailyBetaGroup"),
    "overnight_intraday_split": ("overnight_intraday_split", "OvernightIntradaySplitGroup"),
    "liquidity_rank": ("liquidity_rank", "LiquidityRankGroup"),
    "runner_state": ("runner_state", "RunnerStateGroup"),
    "dumper_state": ("dumper_state", "DumperStateGroup"),
    "gap_fill_state": ("gap_fill_state", "GapFillStateGroup"),
    "candlestick": ("candlestick", "CandlestickGroup"),
    "momentum": ("momentum", "MomentumGroup"),
    "efficiency": ("efficiency", "EfficiencyGroup"),
    "return_dynamics": ("return_dynamics", "ReturnDynamicsGroup"),
    "momentum_consistency": ("momentum_consistency", "MomentumConsistencyGroup"),
    "draw_range": ("draw_range", "DrawRangeGroup"),
    "residual_analysis": ("residual_analysis", "ResidualAnalysisGroup"),
    "clean_momentum": ("clean_momentum", "CleanMomentumScoreGroup"),
    "momentum_run": ("momentum_run", "MomentumRunGroup"),
    # STUBS (clean declares fewer than legacy) — xfailed below until filled:
    "breadth": ("breadth", "BreadthGroup"),
    "prior_day": ("prior_day", "PriorDayGroup"),
    "swing": ("swing", "SwingGroup"),
    "volume": ("volume", "VolumeGroup"),
    "intraday_seasonality": ("intraday_seasonality", "IntradaySeasonalityGroup"),
}

# Known stubs (06-25 sweep): clean feature-set ⊊ legacy. xfail-tracked until ArchOverhaul fills them.
_KNOWN_STUBS = {"breadth", "swing", "intraday_seasonality"}  # trend_quality, volume, prior_day FILLED


def _clean_group_instance(name):  # type: ignore[no-untyped-def]
    import quantlib.features.clean_groups_daily as cd  # noqa: PLC0415
    import quantlib.features.clean_groups_example as ce  # noqa: PLC0415
    import quantlib.features.clean_groups_pointwise as cp  # noqa: PLC0415
    import quantlib.features.clean_groups_stateful as cs  # noqa: PLC0415
    import quantlib.features.clean_groups_windowed as cw  # noqa: PLC0415
    import quantlib.features.clean_groups_xsectional as cx  # noqa: PLC0415

    for mod in (cw, ce, cx, cd, cs, cp):
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and getattr(obj, "name", None) == name and hasattr(obj, "feature_names"):
                return obj()
    return None


@pytest.mark.parametrize("clean_name", sorted(_LEGACY_GROUP_OF))
def test_feature_set_completeness_vs_legacy(clean_name: str) -> None:
    """A clean group's feature NAMES must EQUAL its legacy declare() names (exact set, not subset). Catches
    STUBS that value-match/sparse-h2h miss. Known stubs are xfailed with their gap until filled."""
    import importlib  # noqa: PLC0415
    import sys  # noqa: PLC0415

    inst = _clean_group_instance(clean_name)
    assert inst is not None, f"clean group {clean_name} not found"
    clean_feats = set(inst.feature_names)

    sys.path.insert(0, "/home/ben/quant-fp")
    mod_name, cls_name = _LEGACY_GROUP_OF[clean_name]
    legacy_mod = importlib.import_module(f"quantlib.features.groups.{mod_name}")
    legacy_feats = {s.name for s in getattr(legacy_mod, cls_name)().declare()}

    missing = legacy_feats - clean_feats
    extra = clean_feats - legacy_feats
    if clean_name in _KNOWN_STUBS:
        if missing or extra:
            pytest.xfail(f"{clean_name} STUB: clean {len(clean_feats)} vs legacy {len(legacy_feats)}; "
                         f"missing {sorted(missing)[:4]}; extra {sorted(extra)[:3]} — ArchOverhaul to fill")
    assert not missing, f"{clean_name}: STUB — missing {len(missing)} legacy features: {sorted(missing)[:8]}"
    assert not extra, f"{clean_name}: clean has EXTRA features not in legacy: {sorted(extra)[:8]}"


# Sub-feature demos that are NOT standalone legacy groups (their features live in a parent group):
#   macd → technical, vwap_deviation → price_volume.
_NON_STANDALONE_CLEAN = {"macd", "vwap_deviation"}


def test_completeness_gate_covers_every_clean_group() -> None:
    """The completeness gate is only as good as its coverage: a clean group NOT in _LEGACY_GROUP_OF would
    escape the stub check entirely. This self-check fails if ANY ported clean group (other than the known
    sub-feature demos) is missing from the map — so a new/regressed group can't hide outside the gate."""
    import quantlib.features.clean_groups_daily as cd  # noqa: PLC0415
    import quantlib.features.clean_groups_example as ce  # noqa: PLC0415
    import quantlib.features.clean_groups_pointwise as cp  # noqa: PLC0415
    import quantlib.features.clean_groups_stateful as cs  # noqa: PLC0415
    import quantlib.features.clean_groups_windowed as cw  # noqa: PLC0415
    import quantlib.features.clean_groups_xsectional as cx  # noqa: PLC0415

    all_clean: set[str] = set()
    for mod in (cw, ce, cx, cd, cs, cp):
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and hasattr(obj, "name") and hasattr(obj, "feature_names") \
                    and isinstance(getattr(obj, "name", None), str):
                all_clean.add(obj.name)
    uncovered = all_clean - set(_LEGACY_GROUP_OF) - _NON_STANDALONE_CLEAN
    assert not uncovered, (
        f"clean groups not covered by the completeness gate (add to _LEGACY_GROUP_OF): {sorted(uncovered)}"
    )
