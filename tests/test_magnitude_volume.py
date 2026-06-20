"""Phase-1 magnitude/volume archetype tests — the partial-r own-vol control + the anti-cheat proofs.

The headline (`test_partial_r_catches_vol_persistence`): a signal that predicts the magnitude target
ONLY through the name's own baseline vol must COLLAPSE under the partial-r control (collapse ~0,
verdict DESCRIPTIVE-ONLY) — the #187/#197 anti-fooling lever, the one that caught "most magnitude edge
is vol-persistence". The complementary `test_partial_r_passes_net_new` proves a genuinely independent
factor does NOT collapse. Plus the anti-cheat proofs extended to a magnitude/volume target.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from quantlib.battery.magnitude_volume import (
    TargetKind,
    _net_of_cost_median,
    _residualize_on_own_vol,
    evaluate_magnitude_volume,
)
from quantlib.battery.panel import Panel
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon, Sizing

_SPEC = ArchetypeSpec("magnitude", Horizon.OVERNIGHT, Conditioner.NONE, Sizing.EW)


def _panel(signal: np.ndarray, target: np.ndarray, own_vol: np.ndarray, n_syms: int, n_days: int) -> Panel:
    """A daily panel with one signal column, a magnitude target (fwd_rv_5d), and the own-vol baseline."""
    base = dt.datetime(2025, 1, 5, 19, 59, tzinfo=dt.timezone.utc)
    sc, mn = [], []
    for d in range(n_days):
        ts = int((base + dt.timedelta(days=d)).timestamp() * 1e9)
        for s in range(n_syms):
            sc.append(s)
            mn.append(ts)
    order = sorted(range(len(sc)), key=lambda i: (sc[i], mn[i]))
    return Panel(
        symbol_code=np.array([sc[i] for i in order], dtype=np.int64),
        symbol_names=[f"S{i}" for i in range(n_syms)],
        minute_epoch=np.array([mn[i] for i in order], dtype=np.int64),
        feature_names=["sig"],
        feature_matrix=signal[order].reshape(-1, 1),
        entry_close=np.full(len(order), 50.0),
        half_spread_bps=np.full(len(order), 3.0),
        high=np.full(len(order), 51.0),
        low=np.full(len(order), 49.0),
        volume=np.full(len(order), 1e6),
        extra={"fwd_rv_5d": target[order], "own_rv_20d": own_vol[order]},
        cadence="daily",
    )


def _vol_persistence_panel(seed: int) -> Panel:
    """Signal predicts the target ONLY through own-vol (no net-new) — must collapse."""
    rng = np.random.default_rng(seed)
    n_syms, n_days = 80, 40
    sig, tgt, vol = [], [], []
    for _ in range(n_days):
        v = np.exp(rng.normal(0, 0.5, n_syms))
        for s in range(n_syms):
            vol.append(v[s])
            sig.append(np.log(v[s]) + rng.normal(0, 0.3))
            tgt.append(v[s] * np.exp(rng.normal(0, 0.2)))
    return _panel(np.array(sig), np.array(tgt), np.array(vol), n_syms, n_days)


def _net_new_panel(seed: int) -> Panel:
    """Signal tracks a factor INDEPENDENT of own-vol that also drives the target — must NOT collapse."""
    rng = np.random.default_rng(seed)
    n_syms, n_days = 80, 40
    sig, tgt, vol = [], [], []
    for _ in range(n_days):
        v = np.exp(rng.normal(0, 0.5, n_syms))
        extra = rng.normal(0, 1, n_syms)
        for s in range(n_syms):
            vol.append(v[s])
            sig.append(extra[s] + rng.normal(0, 0.2))
            tgt.append(v[s] * np.exp(0.5 * extra[s] + rng.normal(0, 0.2)))
    return _panel(np.array(sig), np.array(tgt), np.array(vol), n_syms, n_days)


# --- THE partial-r own-vol control ----------------------------------------------------------------


def test_partial_r_catches_vol_persistence() -> None:
    """A signal predictive ONLY via own-vol -> high raw IC, COLLAPSE ~0, DESCRIPTIVE-ONLY."""
    result = evaluate_magnitude_volume(_vol_persistence_panel(0), _SPEC, TargetKind.RV, seed=13)
    assert result.raw_ic > 0.3  # it DOES predict raw
    assert result.collapse < 0.30  # ... but collapses under own-vol -> vol-persistence
    assert result.verdict == "DESCRIPTIVE-ONLY"
    assert "vol-persistence" in result.verdict_reason


def test_partial_r_passes_net_new() -> None:
    """A factor INDEPENDENT of own-vol must NOT collapse (collapse high, residual survives)."""
    result = evaluate_magnitude_volume(_net_new_panel(1), _SPEC, TargetKind.RV, seed=13)
    assert result.raw_ic > 0.2
    assert result.collapse >= 0.30  # net-new — survives the own-vol control
    assert abs(result.resid_ic) > 0.1


def test_residualize_removes_own_vol_dependence() -> None:
    """The residualizer must strip the own-vol component: a pure-own-vol vector residualizes to ~0 IC."""
    rng = np.random.default_rng(2)
    own = np.exp(rng.normal(0, 0.5, 500))
    values = 2.0 * np.log(own) + rng.normal(0, 0.01, 500)  # almost pure own-vol
    resid = _residualize_on_own_vol(values, own)
    assert np.nanstd(resid) < np.std(values)  # variance removed
    assert abs(np.corrcoef(resid[np.isfinite(resid)], np.log(own)[np.isfinite(resid)])[0, 1]) < 0.05


# --- anti-cheat extended to magnitude/volume ------------------------------------------------------


def test_noise_signal_no_magnitude_edge() -> None:
    """A pure-noise signal must show ~0 raw IC against the magnitude target (no manufactured edge)."""
    panel = _net_new_panel(3)
    rng = np.random.default_rng(99)
    panel.feature_matrix[:, 0] = rng.normal(0, 1, panel.n_rows)  # replace signal with noise
    result = evaluate_magnitude_volume(panel, _SPEC, TargetKind.RV, seed=13)
    assert abs(result.raw_ic) < 0.05
    assert result.verdict == "DESCRIPTIVE-ONLY"


def test_shuffle_canary_collapses_on_magnitude() -> None:
    """The within-timestamp shuffle canary must be ~0 even on the net-new panel (leakage arbiter)."""
    result = evaluate_magnitude_volume(_net_new_panel(4), _SPEC, TargetKind.RV, seed=13)
    assert abs(result.shuffle_ic) < 0.05


# --- the net-of-cost median gate ------------------------------------------------------------------


def test_net_of_cost_median_is_negative_when_move_does_not_beat_baseline() -> None:
    """The #197 gate: when the realized |move| ~ the own-baseline expected move, the MEDIAN event does
    NOT beat baseline + round-trip cost -> median net negative."""
    rng = np.random.default_rng(5)
    own = np.abs(rng.normal(0.02, 0.005, 500)) + 1e-3
    absret = own * np.exp(rng.normal(0, 0.3, 500))  # realized ~ baseline (lognormal around it)
    net_bps, win = _net_of_cost_median(absret, own)
    assert net_bps < 0.0  # the typical event loses to the straddle cost
    assert win < 0.5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
