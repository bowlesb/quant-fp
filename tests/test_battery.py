"""Unit tests for the strategy-battery Phase 0 (the cross-sectional battery).

Covers the discipline pieces that must be un-foolable WITHOUT needing the store or LightGBM:
the SanityReport guards, the two null baselines, the per-name cost model + cost curve, the
BY-FDR family correction, and the verdict decision tree. A synthetic in-memory `Panel` exercises
the full `CrossSectionalLS.backtest` path with the raw-feature (no-GBM) fast path so the test is
fast and deterministic.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from quantlib.battery.family import benjamini_yekutieli, one_sided_p_from_t
from quantlib.battery.panel import Panel
from quantlib.battery.result import (
    BacktestResult,
    NullStat,
    SanityReport,
    Verdict,
    decide_verdict,
)
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon, Sizing
from quantlib.battery.strategy import CrossSectionalLS, _per_day_winsor_excess
from quantlib.strategy_core.cost import cost_curve, long_short_per_name_cost


def _epoch(day_offset: int) -> int:
    base = dt.datetime(2025, 1, 2, 19, 59, tzinfo=dt.timezone.utc)
    return int((base + dt.timedelta(days=day_offset)).timestamp() * 1e9)


def _make_intraday_panel(n_days: int, n_symbols: int, signal_strength: float, seed: int) -> Panel:
    """Synthetic intraday panel: feature_0 carries `signal_strength` * forward excess + noise.
    signal_strength=0 -> pure noise (must produce ~0 edge); >0 -> a recoverable edge."""
    rng = np.random.default_rng(seed)
    symbol_codes: list[int] = []
    minutes: list[int] = []
    feature_vals: list[float] = []
    labels: list[float] = []
    spreads: list[float] = []
    closes: list[float] = []
    dollar_vols: list[float] = []
    for day in range(n_days):
        raw = rng.normal(0, 0.02, n_symbols)
        excess = raw - np.median(raw)
        feat = signal_strength * excess + rng.normal(0, 0.02, n_symbols)
        for sym in range(n_symbols):
            symbol_codes.append(sym)
            minutes.append(_epoch(day))
            feature_vals.append(float(feat[sym]))
            labels.append(float(excess[sym]))
            spreads.append(2.0 + 8.0 * (sym / n_symbols))  # low-code names trade tighter
            closes.append(50.0)
            dollar_vols.append(1e8 * (n_symbols - sym))
    # sort by (symbol, minute)
    order = sorted(range(len(symbol_codes)), key=lambda i: (symbol_codes[i], minutes[i]))
    sc = np.array([symbol_codes[i] for i in order], dtype=np.int64)
    mn = np.array([minutes[i] for i in order], dtype=np.int64)
    fm = np.array([[feature_vals[i]] for i in order], dtype=float)
    lab = np.array([labels[i] for i in order], dtype=float)
    panel = Panel(
        symbol_code=sc,
        symbol_names=[f"S{i}" for i in range(n_symbols)],
        minute_epoch=mn,
        feature_names=["feature_0"],
        feature_matrix=fm,
        entry_close=np.array([closes[i] for i in order], dtype=float),
        half_spread_bps=np.array([spreads[i] for i in order], dtype=float),
        high=np.full(len(order), 51.0),
        low=np.full(len(order), 49.0),
        volume=np.full(len(order), 1e6),
        extra={
            "fwd_30m": lab,
            "rth_dollar_vol": np.array([dollar_vols[i] for i in order], dtype=float),
            "up_market_day": np.ones(len(order), dtype=bool),
        },
        cadence="intraday",
    )
    return panel


# --- SanityReport guards (trap #3) ---------------------------------------------------------------


def test_sanity_flags_label_std_blowup() -> None:
    """The 50-226x fake-return blow-up (label_std ~0.77) must trip the guard."""
    report = SanityReport(
        price_floor_applied=True,
        winsorized=True,
        label_std=0.77,
        label_std_ok=False,
        entry_minute_ok=True,
        tradeable_fraction=1.0,
    )
    assert not report.ok
    assert "label_std" in report.reason


def test_sanity_flags_early_entry() -> None:
    report = SanityReport(
        price_floor_applied=True,
        winsorized=True,
        label_std=0.02,
        label_std_ok=True,
        entry_minute_ok=False,
        tradeable_fraction=1.0,
    )
    assert not report.ok
    assert "09:35" in report.reason


def test_sanity_clean_passes() -> None:
    report = SanityReport(
        price_floor_applied=True,
        winsorized=True,
        label_std=0.02,
        label_std_ok=True,
        entry_minute_ok=True,
        tradeable_fraction=1.0,
    )
    assert report.ok
    assert report.reason == "clean"


# --- Verdict decision tree -----------------------------------------------------------------------


def _result(**kw: object) -> BacktestResult:
    defaults = dict(
        spec=ArchetypeSpec("cross_sectional_ls", Horizon.OVERNIGHT, Conditioner.NONE, Sizing.EW),
        net_per_period=0.001,
        gross_per_period=0.002,
        sharpe_net=1.0,
        hit_rate=0.6,
        mean_turnover=2.0,
        breakeven_cost_bps=20.0,
        shuffle_canary=NullStat(0.0, 100),
        predict_zero=NullStat(0.0, 100),
        edge_vs_shuffle=0.03,
        mean_ic=0.035,
        nw_t=3.89,
        n_test_ts=100,
        n_rows=1000,
        directional=True,
        up_vs_down_asymmetry=None,
        sanity=SanityReport(True, True, 0.02, True, True, 1.0),
    )
    defaults.update(kw)
    return BacktestResult(**defaults)  # type: ignore[arg-type]


def test_verdict_pass_on_strong_edge() -> None:
    verdict, _ = decide_verdict(_result())
    assert verdict == Verdict.PASS


def test_verdict_fail_on_weak_t() -> None:
    verdict, reason = decide_verdict(
        _result(nw_t=1.20, breakeven_cost_bps=4.12, mean_ic=0.011, edge_vs_shuffle=0.0073)
    )
    assert verdict == Verdict.FAIL
    assert "|t|>=2.0" in reason


def test_verdict_descriptive_only_for_nondirectional() -> None:
    verdict, _ = decide_verdict(_result(directional=False))
    assert verdict == Verdict.DESCRIPTIVE_ONLY


def test_verdict_trap_flagged_short_circuits() -> None:
    bad_sanity = SanityReport(True, True, 0.77, False, True, 1.0)
    verdict, _ = decide_verdict(_result(sanity=bad_sanity))
    assert verdict == Verdict.TRAP_FLAGGED


# --- Cost model (trap #1) ------------------------------------------------------------------------


def test_per_name_cost_reduces_net_below_gross() -> None:
    rng = np.random.default_rng(0)
    n = 600
    pred = list(rng.normal(0, 1, n))
    realized = [p * 0.01 + rng.normal(0, 0.005) for p in pred]  # a real edge
    group = [_epoch(i // 60) for i in range(n)]
    symbol = [f"S{i % 60}" for i in range(n)]
    spreads = [5.0] * n
    result = long_short_per_name_cost(
        pred, realized, group, symbol, spreads, frac=0.2, periods_per_year=252.0
    )
    assert result["gross_per_period"] > result["net_per_period"]
    assert result["breakeven_cost_bps"] > 0


def test_cost_curve_monotone_decreasing() -> None:
    rng = np.random.default_rng(1)
    n = 600
    pred = list(rng.normal(0, 1, n))
    realized = [p * 0.01 + rng.normal(0, 0.005) for p in pred]
    group = [_epoch(i // 60) for i in range(n)]
    symbol = [f"S{i % 60}" for i in range(n)]
    spreads = [5.0] * n
    curve = cost_curve(pred, realized, group, symbol, spreads, frac=0.2, periods_per_year=252.0)
    nets = [net for _, net in curve]
    # higher cost multiplier -> lower (or equal) net P&L
    for earlier, later in zip(nets, nets[1:]):
        assert later <= earlier + 1e-9


# --- BY-FDR family correction (§6) ---------------------------------------------------------------


def test_by_fdr_rejects_nothing_on_all_null() -> None:
    fc = benjamini_yekutieli(["a", "b", "c", "d"], [0.6, 0.7, 0.8, 0.9], q=0.10, pre_registered=True)
    assert not any(fc.reject)


def test_by_fdr_rejects_strong_signal() -> None:
    fc = benjamini_yekutieli(["a", "b", "c", "d"], [0.0001, 0.7, 0.8, 0.9], q=0.10, pre_registered=True)
    assert fc.reject[0]
    assert not any(fc.reject[1:])


def test_by_more_conservative_than_uncorrected() -> None:
    """A p just under 0.05 should NOT survive BY across m=4 (the multiple-comparisons defense)."""
    fc = benjamini_yekutieli(["a", "b", "c", "d"], [0.04, 0.6, 0.7, 0.8], q=0.10, pre_registered=True)
    assert not fc.reject[0]


def test_one_sided_p_monotone() -> None:
    assert one_sided_p_from_t(4.0) < one_sided_p_from_t(2.0) < one_sided_p_from_t(0.0)
    assert one_sided_p_from_t(float("nan")) == 1.0


# --- The full backtest path on a synthetic Panel (null baseline) ---------------------------------


def test_pure_noise_panel_produces_no_edge() -> None:
    """signal_strength=0 -> a feature with NO predictive content. The battery's two nulls must
    agree (real IC ~ shuffle IC), and the verdict must be FAIL (the expected honest null)."""
    panel = _make_intraday_panel(n_days=40, n_symbols=80, signal_strength=0.0, seed=7)
    spec = ArchetypeSpec("cross_sectional_ls", Horizon.M30, Conditioner.NONE, Sizing.EW)
    result = CrossSectionalLS(spec, seed=13, use_gbm=False).backtest(panel)
    assert abs(result.mean_ic) < 0.05
    assert abs(result.edge_vs_shuffle) < 0.05
    assert result.verdict == Verdict.FAIL


def test_strong_signal_panel_recovers_edge() -> None:
    """A feature that genuinely ranks forward excess must produce a positive real IC clearly above
    its shuffle canary (the harness can SEE a real edge when one exists)."""
    panel = _make_intraday_panel(n_days=60, n_symbols=120, signal_strength=3.0, seed=3)
    spec = ArchetypeSpec("cross_sectional_ls", Horizon.M30, Conditioner.NONE, Sizing.EW)
    result = CrossSectionalLS(spec, seed=13, use_gbm=False).backtest(panel)
    assert result.mean_ic > 0.1
    assert result.edge_vs_shuffle > 0.1
    assert result.shuffle_canary.ic == result.shuffle_canary.ic  # not NaN


def test_predict_zero_baseline_is_zero() -> None:
    panel = _make_intraday_panel(n_days=30, n_symbols=60, signal_strength=1.0, seed=5)
    spec = ArchetypeSpec("cross_sectional_ls", Horizon.M30, Conditioner.NONE, Sizing.EW)
    result = CrossSectionalLS(spec, seed=13, use_gbm=False).backtest(panel)
    assert result.predict_zero.ic == 0.0


# --- The per-day winsor + excess label transform (trap #3) ---------------------------------------


def test_winsor_excess_clips_outlier_and_demeans() -> None:
    """Per-day symmetric winsorization (0.5%/99.5%) is the SECONDARY tail-trim (the $1 price floor
    is the primary guard, applied earlier in the Panel build). Here we assert what winsor itself
    guarantees: a lone blow-up far outside the 99.5th percentile is pulled IN to that percentile
    (orders of magnitude smaller), and the cross-section is demeaned to ~0 median."""
    rng = np.random.default_rng(0)
    bulk = rng.normal(0.0, 0.02, 400)  # a realistic ~400-name cross-section
    raw = np.concatenate([bulk, np.array([100.0])])  # one bad sub-penny-print blow-up
    day_code = np.zeros(raw.size, dtype=np.int64)
    out = _per_day_winsor_excess(raw, day_code)
    assert np.isfinite(out).all()
    # the 100x print is pulled in toward the bulk's 99.5th percentile (< 0.2), not left at 100
    assert out.max() < 0.2
    assert abs(np.median(out)) < 0.05  # demeaned


def test_winsor_excess_nulls_thin_cross_section() -> None:
    raw = np.array([0.01, -0.01, 0.02], dtype=float)  # below MIN_CROSS_SECTION=20
    day_code = np.zeros(raw.size, dtype=np.int64)
    out = _per_day_winsor_excess(raw, day_code)
    assert np.isnan(out).all()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
