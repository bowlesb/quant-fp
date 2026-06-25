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
from quantlib.features.clean_groups_example import TrendQualityClean, VwapDeviationClean

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
