"""Incremental fast path wired into ``capture.process_bars`` == the batch path, cell-for-cell.

This is the live-integration parity gate for P1 #1 (per-symbol fast path). ``process_bars`` is the shared
compute core for the mock, real, and sharded capture clients; with ``FP_INCREMENTAL=1`` it assembles the
batched ``ReductionGroup``s from per-bucket ``IncrementalEngine`` running sums (via ``step`` — the SAME
``assemble_from_long`` the batch uses, so warmup/flag null handling is byte-identical) instead of
recomputing the whole buffer each minute. Parity is sacred (CLAUDE.md): the incremental output must equal
the batch output within tolerance, under a FLUCTUATING active symbol set (the live regime).

Default (no env set) the path is byte-identical to the batch — guaranteed by ``test_default_is_batch``.

KNOWN CONDITIONING CAVEAT (``test_ols_r2_near_perfect_fit_is_flagged``): sum-based OLS r2/corr near a
PERFECT fit is a difference of large near-equal sums; the incremental running add/subtract rounds
differently from the batch's fresh window sums, so r2≈1 can diverge far beyond tolerance. The self-check
exists to SURFACE exactly that before the fast path is trusted as the source.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from quantlib.features import capture
from quantlib.features.base import BatchContext
from quantlib.features.capture import (
    _PARITY_BREACH_RATIO,
    CaptureState,
    MinuteRing,
    _bars_to_frame,
    _incremental_config,
    _incremental_parity,
    process_bars,
)
from quantlib.features.declarative import ReductionGroup, compute_reduction_batch
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.registry import REGISTRY

BASE = dt.datetime(2026, 6, 16, 14, 0, tzinfo=dt.timezone.utc)

# The conditioning-sensitive groups gated to the batch fresh-sum recompute, each DEMONSTRATED to breach the
# engine-vs-batch parity self-check on a gappy/near-flat walk by test_gappy_denom_group_breaches_raw_so_gate_is_
# load_bearing below (worst ratio + null/non-null mismatch recorded there). ``volume`` (variance-family std:
# power-sum sqrt vs backfill rolling_std_by FORMULA gap) is the original. ``return_dynamics`` (lagged-return
# autocorrelation) and ``volume_leads_price`` (lagged-volume×return correlation) breach because the OLS pairing
# count and cross-product running sums round across the corr defined-guard differently from the batch fresh sum
# when the paired series collapse on a sparse window. ``price_volume`` reverts to gated (was #155 True): its
# ``pv_correlation`` regresses return on RAW share volume and breaches BEYOND the b==2 perfect-fit corner the
# n==2 guard closed (77x tolerance + 6 null mismatches in the sweep). ``market_beta`` regresses each symbol's
# return on SPY's broadcast return; on a gappy symbol the few paired bars give a near-constant SPY-return x, so
# the corr denominator collapses the SAME way — the real-06-18 A/B (test_market_beta_breaches_on_real_gappy_
# spy_regressor) flagged market_corr_*=±1 / idio_vol_*=0 where batch NULLs on MO/SLB. The shared centered-denom
# kernel (fingerprint-affecting, Lead-coordinated) is the queued follow-up to widen incremental coverage here.
#
# NOT gated (verified parity-clean on the real-06-18 gappy A/B, no null mismatch, worst < 4e-4x tolerance):
# distribution, volatility — their power-sum-moment / std algebra does NOT collapse the way the correlation-of-
# two-sparse-series groups do. The task's draft gate list also named market_beta with these; the SYNTHETIC
# gappy sweep cleared all three, but the REAL-DATA A/B reconciliation showed market_beta breaches (the synthetic
# had no SPY regressor so its corr-denom was never exercised) while distribution/volatility stayed clean.
INCREMENTAL_UNSAFE = {
    # return_dynamics + volume_leads_price were UNGATED by P2 (#283): the Neumaier compensated running sum
    # closes their corr-denom straddle (engine-vs-batch CLEAN, 0/295 adversarial; guarded by
    # test_gappy_denom_group_now_clean_after_p2_neumaier), so they now ride the incremental path.
    "volume",  # batch-vs-canonical std FORMULA gap (drift-independent — Neumaier does NOT close it; verified it
    # still breaches at intermediate volume variance v=5e6*(1+N(0,1e-5)), rel~15). Needs the centered-std batch fix.
    "price_volume",
    "market_beta",
    # residual_analysis: the OLS residual SSR is a difference of large near-equal centered power sums on a
    # near-perfect intraday fit (the same cancellation as price_r2), so the incremental running sums round past
    # the parity-breach ratio. Stays on the batch fresh-sum path until the centered-power-sum kernel lands.
    "residual_analysis",
}
# Genuinely safe sum-ratio / time-axis-OLS groups that ride the incremental fast path. trend_quality and
# clean_momentum regress close on a CENTERED TIME axis (x always spreads with the minutes, never collapses on a
# gappy window) — well-conditioned — and were proven parity-true on degenerate walks by the tests below.
INCREMENTAL_FLIPPED = {"trend_quality", "clean_momentum"}


def _stream_minutes(n_sym: int, n_min: int, present_p: float, seed: int, vol: float = 0.02) -> list[list[dict]]:
    """A normalized-bar stream (list of per-minute bar batches) where minute 0 carries every symbol (clean
    warmup) and each later minute carries a random ~``present_p`` subset — the live membership-churn shape.
    ``vol`` sets the per-minute return noise; the default 0.02 keeps regressions well-conditioned (r2 well
    below 1) so the parity comparison is not dominated by sum-based r2 cancellation near a perfect fit."""
    rng = np.random.default_rng(seed)
    price = {s: 100.0 + s for s in range(n_sym)}
    out: list[list[dict]] = []
    for mi in range(n_min):
        minute_iso = (BASE + dt.timedelta(minutes=mi)).isoformat()
        bars: list[dict] = []
        for s in range(n_sym):
            present = mi == 0 or rng.random() < present_p
            price[s] *= 1.0 + (rng.standard_normal() * vol)
            if not present:
                continue
            c = price[s]
            bars.append({"S": f"S{s}", "o": c * 0.999, "c": c, "h": c * 1.002, "l": c * 0.998,
                         "v": 1000.0 + rng.random() * 4000, "t": minute_iso})
        out.append(bars)
    return out


def _flat_volume_minutes(
    n_sym: int, n_min: int, present_p: float, seed: int, vol: float, vol_noise: float = 1e-5
) -> list[list[dict]]:
    """A stream with NEAR-CONSTANT huge share volume (the worst variance-cancellation regime for volume_zscore:
    Σv² and (Σv)²/n are large near-equal sums whose difference is float noise, so the power-sum std flips
    across the relative null-floor differently from backfill's rolling_std_by). Price still drifts normally.

    ``vol_noise`` is the RELATIVE volume jitter (std as a fraction of the ~5e6 level). The default 1e-5 is the
    INTERMEDIATE-variance regime that genuinely breaches: it was a measured FALSE-GREEN trap that the prior
    near-ZERO-variance ``v = 5e6 + N(0,1)`` form (vol_noise ~ 2e-7) hid — at that tiny variance the power-sum
    std collapses to the null-floor identically on both paths (spuriously clean), while at 1e-5 the
    ``Σv²−(Σv)²/n`` cancellation lands the z-score ~3e-5 apart (rel ~15x the parity ratio). The test asserts
    the breach at this regime so the gate cannot false-pass on a too-quiet stream again."""
    rng = np.random.default_rng(seed)
    price = {s: 100.0 + s for s in range(n_sym)}
    base_vol = {s: rng.uniform(5e5, 5e6) for s in range(n_sym)}
    out: list[list[dict]] = []
    for mi in range(n_min):
        minute_iso = (BASE + dt.timedelta(minutes=mi)).isoformat()
        bars: list[dict] = []
        for s in range(n_sym):
            present = mi == 0 or rng.random() < present_p
            price[s] *= 1.0 + (rng.standard_normal() * vol)
            if not present:
                continue
            c = price[s]
            # near-constant huge volume with a RELATIVE (not additive-±1) jitter — the regime that exposes the
            # batch-vs-canonical std cancellation (additive ±1 noise on 5e6 is below the breach threshold).
            v = max(base_vol[s] * (1.0 + rng.standard_normal() * vol_noise), 1.0)
            bars.append({"S": f"S{s}", "o": c * 0.999, "c": c, "h": c * 1.002, "l": c * 0.998,
                         "v": v, "t": minute_iso})
        out.append(bars)
    return out


def _run(stream: list[list[dict]], root: str) -> dict[str, pl.DataFrame]:
    """Drive a stream through ``process_bars`` (accumulate in RAM, no store) and return the per-group output."""
    state = CaptureState()
    for bars in stream:
        process_bars(state, bars, root, "mock", "2026-06-16", 120, accumulate=True, write=False)
    return state.accumulated


def _worst_tol_ratio(batch: dict, inc: dict, *, cols_drop: tuple[str, ...] = ()) -> float:
    """Worst divergence between two accumulated per-group dicts, as a multiple of the parity tolerance,
    joined on (symbol, minute). ``cols_drop`` excludes named feature columns (used to isolate the r2 family)."""
    assert set(batch) == set(inc), "group set differs"
    worst = 0.0
    for name, bframe in batch.items():
        iframe = inc[name]
        assert set(bframe.columns) == set(iframe.columns), f"{name}: columns differ"
        keys = ["symbol", "minute"]
        cols = [c for c in bframe.columns if c not in keys and not any(d in c for d in cols_drop)]
        j = bframe.sort(keys).join(iframe.sort(keys).select([*keys, *cols]), on=keys, suffix="__i")
        assert j.height == bframe.height, f"{name}: row set differs"
        for col in cols:
            a, b = pl.col(col), pl.col(f"{col}__i")
            assert j.filter(a.is_null() != b.is_null()).height == 0, f"{name}.{col}: null/non-null mismatch"
            ratio = j.select(((a - b).abs() / (1e-6 + 1e-6 * a.abs())).fill_null(0.0).max()).item()
            if ratio is not None:
                worst = max(worst, float(ratio))
    return worst


def test_default_is_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env set the incremental path is inert: config reads all-off (output is the batch output)."""
    for var in ("FP_INCREMENTAL", "FP_INCREMENTAL_PARITY", "FP_INCREMENTAL_SLICE"):
        monkeypatch.delenv(var, raising=False)
    assert _incremental_config() == (False, False, False)


def test_incremental_capture_matches_batch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """FP_INCREMENTAL=1 (gap-safe whole-buffer derive) produces the SAME per-group output as the batch path
    on well-conditioned data, under a fluctuating active symbol set across many minutes — within a small
    multiple of the parity tolerance (benign float drift)."""
    stream = _stream_minutes(n_sym=8, n_min=50, present_p=0.7, seed=3)

    monkeypatch.delenv("FP_INCREMENTAL", raising=False)
    batch = _run(stream, str(tmp_path / "batch"))

    monkeypatch.setenv("FP_INCREMENTAL", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)  # gap-safe whole-buffer derive (open slice constraint)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(stream, str(tmp_path / "inc"))

    assert batch, "expected reduction-group output"
    worst = _worst_tol_ratio(batch, inc)
    assert worst < _PARITY_BREACH_RATIO, f"incremental diverged from batch: worst {worst}x tolerance"


def test_parity_selfcheck_records_clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """FP_INCREMENTAL=1 + FP_INCREMENTAL_PARITY=1 runs BOTH paths each minute, writes the batch truth, and
    records a within-drift divergence (no breach) on well-conditioned data. Exercises the self-check wiring."""
    stream = _stream_minutes(n_sym=6, n_min=40, present_p=0.7, seed=9)
    seen: list[tuple[str, float, bool]] = []
    monkeypatch.setattr(capture.metrics, "record_incremental_parity",
                        lambda ri, r, b: seen.append((ri, r, b)))
    monkeypatch.setenv("FP_INCREMENTAL", "1")
    monkeypatch.setenv("FP_INCREMENTAL_PARITY", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)

    _run(stream, str(tmp_path / "selfcheck"))

    assert seen, "self-check should record a parity sample each minute"
    assert not any(breached for _, _, breached in seen), \
        f"no minute should breach on well-conditioned data; worst {max(r for _, r, _ in seen)}x tolerance"


@pytest.mark.parametrize("slice_derive", [True, False])
def test_ols_near_perfect_fit_is_parity_true(slice_derive: bool) -> None:
    """The former KNOWN CONDITIONING CAVEAT, now CLOSED at source. On a SMOOTH (near-linear) price walk the
    OLS R²/correlation family fits near-perfectly (R²→1, the b==2 corner is exact) — the historical breach
    source. With the time-OLS origin-rebase (PR #132) and the n==2 perfect-fit guard (_OLS_PERFECT_FIT_COUNT:
    r2=1.0 / corr=sign(cov) at b==2), the IncrementalEngine now agrees with the batch CELL-FOR-CELL on the
    flipped TIME-axis-OLS groups (trend_quality, clean_momentum) — well under the breach ratio on BOTH the live
    slice-derive and the whole-buffer paths. This is the parity proof gating their ``incremental_safe = True``
    flip. (``price_volume`` is gated to batch — its pv_correlation regresses on raw volume, not time.)"""
    smooth = _stream_minutes(n_sym=8, n_min=20, present_p=0.7, seed=3, vol=0.002)  # near-linear -> near-perfect fit
    flipped = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name in INCREMENTAL_FLIPPED]
    assert {g.name for g in flipped} == INCREMENTAL_FLIPPED, "flipped OLS groups missing from registry"
    assert all(g.incremental_safe for g in flipped), "flipped OLS groups must be incremental_safe=True"
    ring = MinuteRing(maxlen=120)
    engine: IncrementalEngine | None = None
    worst = 0.0
    for bars in smooth:
        if not bars:
            continue
        ring.push(_bars_to_frame(bars))
        frame = ring.materialize()
        ctx = BatchContext(frames={"minute_agg": frame})
        batch = compute_reduction_batch(flipped, ctx)
        if engine is None:
            engine = IncrementalEngine(flipped)
        inc = engine.step(frame, slice_derive=slice_derive)
        worst = max(worst, _worst_tol_ratio(batch, inc))
    assert worst < _PARITY_BREACH_RATIO, f"flipped OLS groups must be parity-true on perfect-fit walk: {worst}x"


@pytest.mark.parametrize(
    "seed,present_p,vol",
    [
        (5, 0.30, 0.0002),  # very sparse + ultra-smooth: many b==2 perfect-fit windows
        (11, 0.45, 0.0005),  # b==2-heavy churn
        (23, 0.35, 0.0003),  # sparse near-linear
        (7, 0.70, 0.020),  # well-conditioned (control)
    ],
)
def test_flipped_ols_groups_parity_on_degenerate_walks(seed: int, present_p: float, vol: float) -> None:
    """Cell-for-cell batch==incremental on the FLIPPED OLS groups across degenerate regimes (n==2 perfect-fit
    corners from sparse presence, near-flat ultra-smooth walks) AND a well-conditioned control — on the LIVE
    slice-derive path. The n==2 perfect-fit guard + origin-rebase make the OLS r2/corr family parity-true by
    construction: no seed/regime breaches and no null/non-null mismatch (the helper asserts the latter)."""
    walk = _stream_minutes(n_sym=10, n_min=18, present_p=present_p, seed=seed, vol=vol)
    flipped = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name in INCREMENTAL_FLIPPED]
    ring = MinuteRing(maxlen=120)
    engine: IncrementalEngine | None = None
    worst = 0.0
    for bars in walk:
        if not bars:
            continue
        ring.push(_bars_to_frame(bars))
        frame = ring.materialize()
        ctx = BatchContext(frames={"minute_agg": frame})
        batch = compute_reduction_batch(flipped, ctx)
        if engine is None:
            engine = IncrementalEngine(flipped)
        inc = engine.step(frame, slice_derive=True)
        worst = max(worst, _worst_tol_ratio(batch, inc))
    assert worst < _PARITY_BREACH_RATIO, f"flipped OLS breached on degenerate walk (seed={seed}): {worst}x"


def _late_appearance_minutes(n_sym: int, n_min: int, present_p: float, seed: int, vol: float) -> list[list[dict]]:
    """A sparse stream where NO minute carries every symbol — symbols FIRST appear at random later minutes (NOT
    a clean minute-0 warmup). This drives the ``SymbolSetExpanded`` re-seed (a genuinely-new ticker each time a
    fresh symbol appears), folding the whole multi-minute buffer through ``_matrix_at`` for HISTORICAL minutes —
    the path that exposed the rust slice-derive future-row lag leak (a first-bar return wrongly non-null →
    double-counted OLS pairing → ``pv_correlation`` ±1.0 in incremental vs ``null`` in batch). Mirrors the live
    crypto regime (1-5 symbols/minute) the FP_INCREMENTAL crypto soak breached on."""
    rng = np.random.default_rng(seed)
    price = {s: 100.0 + s for s in range(n_sym)}
    out: list[list[dict]] = []
    for mi in range(n_min):
        minute_iso = (BASE + dt.timedelta(minutes=mi)).isoformat()
        bars: list[dict] = []
        for s in range(n_sym):
            price[s] *= 1.0 + (rng.standard_normal() * vol)
            if rng.random() >= present_p:  # no minute-0 force: symbols appear (and reappear) sparsely
                continue
            c = price[s]
            bars.append({"S": f"S{s}", "o": c * 0.999, "c": c, "h": c * 1.002, "l": c * 0.998,
                         "v": 1000.0 + rng.random() * 4000, "t": minute_iso})
        if not bars:  # a real feed always carries >=1 symbol per minute
            s = int(rng.integers(n_sym))
            c = price[s]
            bars.append({"S": f"S{s}", "o": c * 0.999, "c": c, "h": c * 1.002, "l": c * 0.998,
                         "v": 1000.0 + rng.random() * 4000, "t": minute_iso})
        out.append(bars)
    return out


@pytest.mark.parametrize(
    "seed,present_p,vol",
    [
        (1, 0.40, 0.005),  # sparse, symbols first-appear late -> repeated re-seed over multi-minute buffer
        (2, 0.25, 0.005),  # sparser (crypto-like 1-5 sym/min)
        (3, 0.15, 0.005),  # very sparse: most windows are first-appearance b==1/b==2 corners
    ],
)
def test_flipped_ols_parity_on_late_appearance_reseed(seed: int, present_p: float, vol: float) -> None:
    """Cell-for-cell batch==incremental on the FLIPPED OLS groups when symbols FIRST APPEAR at later minutes
    (no clean minute-0 warmup), so each new ticker triggers a ``SymbolSetExpanded`` re-seed that folds the whole
    buffer through the slice-derive path for HISTORICAL minutes. Regression for the rust slice-derive future-row
    lag leak: without the ``<= minute`` point-in-time cut a first-appearance return's prior-close lag came back
    non-null, double-counting the OLS pairing ``b`` and emitting ``pv_correlation`` ±1.0 (the n==2 perfect-fit
    value) where the batch correctly emits ``null`` — the FP_INCREMENTAL null/non-null A/B breach the crypto
    soak found. The helper ``_worst_tol_ratio`` returns ``inf`` on any null/non-null mismatch, so a re-breach
    fails here loudly."""
    walk = _late_appearance_minutes(n_sym=20, n_min=40, present_p=present_p, seed=seed, vol=vol)
    flipped = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name in INCREMENTAL_FLIPPED]
    ring = MinuteRing(maxlen=120)
    engine: IncrementalEngine | None = None
    worst = 0.0
    for bars in walk:
        if not bars:
            continue
        ring.push(_bars_to_frame(bars))
        frame = ring.materialize()
        ctx = BatchContext(frames={"minute_agg": frame})
        batch = compute_reduction_batch(flipped, ctx)
        if engine is None:
            engine = IncrementalEngine(flipped)
        inc = engine.step(frame, slice_derive=True)
        worst = max(worst, _worst_tol_ratio(batch, inc))
    assert worst < _PARITY_BREACH_RATIO, (
        f"flipped OLS breached on late-appearance re-seed (seed={seed}, p={present_p}): {worst}x "
        "(inf = a null/non-null mismatch = the sparse first-bar lag leak regressed)"
    )


def test_volume_still_gated_breaches_on_degenerate_variance() -> None:
    """``volume`` STAYS GATED — its z-score std diverges batch-vs-incremental on a near-constant huge-volume
    window. STRENGTHENED (false-green fix): the prior stream used ``v = 5e6 + N(0,1)`` — a near-ZERO relative
    variance at which the power-sum std collapses to the null-floor identically on BOTH paths, spuriously
    passing a ``not breached`` assertion (a P2 Neumaier-fixed claim that was a TEST ARTIFACT, not real). At the
    INTERMEDIATE-variance regime (``vol_noise=1e-5``, std ~1e-5 of the 5e6 level) the ``Σv²−(Σv)²/n``
    cancellation lands the z-score ~3e-5 apart — rel ~15x the parity ratio — a genuine batch-vs-canonical std
    FORMULA gap that Neumaier (a DRIFT fix on the running sums) does NOT close. So the gate is LOAD-BEARING and
    ``incremental_safe = False`` is correct; un-gating volume needs the centered-power-sum std batch change
    (store Σ(v−c)/Σ(v−c)² for a reproducible per-window c so the squared terms are small), a separate engine
    change. Comparing the engine DIRECTLY against the batch (no gate) MUST breach at this regime."""
    walk = _flat_volume_minutes(n_sym=8, n_min=25, present_p=0.7, seed=9, vol=0.01, vol_noise=1e-5)
    volume = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name == "volume"]
    ring = MinuteRing(maxlen=120)
    engine: IncrementalEngine | None = None
    breached = False
    for bars in walk:
        if not bars:
            continue
        ring.push(_bars_to_frame(bars))
        frame = ring.materialize()
        ctx = BatchContext(frames={"minute_agg": frame})
        batch = compute_reduction_batch(volume, ctx)
        if engine is None:
            engine = IncrementalEngine(volume)
        inc = engine.step(frame, slice_derive=True)
        # The breach surfaces as EITHER a null/non-null mismatch at the std floor (the helper asserts on it) OR
        # a large z-score disagreement; both mean the gate is load-bearing.
        try:
            if _worst_tol_ratio(batch, inc) > _PARITY_BREACH_RATIO:
                breached = True
        except AssertionError:
            breached = True
    assert breached, (
        "volume engine-vs-batch must STILL breach on the intermediate-variance huge-volume window (the "
        "batch-vs-canonical std FORMULA gap) — its incremental_safe=False gate is load-bearing. If a "
        "centered-power-sum std fix lands, flip the assertion + un-gate volume."
    )


def _gappy_corr_minutes(n_sym: int, n_min: int, present_p: float, seed: int) -> list[list[dict]]:
    """A GAPPY, near-constant-return stream: symbols skip most minutes (sparse presence) and the price drifts at
    a ~fixed tiny rate, so a trailing window holds only a few bars whose one-minute returns are ~equal. This is
    the conditioning case the 19 smooth-synthetic walks miss: the correlation kernel's paired series (a return
    against its own lag / against lagged volume) collapse to ~constant over the sparse window, so the corr
    denominator √(var_x·var_y) is a difference of near-equal running sums — the incremental running sums round
    it across the corr defined-guard differently from the batch fresh sum, so incremental emits a value where
    batch emits NULL (or disagrees past the breach ratio). Re-paired counts under sparse presence amplify it."""
    rng = np.random.default_rng(seed)
    price = {s: 100.0 + s for s in range(n_sym)}
    out: list[list[dict]] = []
    for mi in range(n_min):
        minute_iso = (BASE + dt.timedelta(minutes=mi)).isoformat()
        bars: list[dict] = []
        for s in range(n_sym):
            present = mi == 0 or rng.random() < present_p
            price[s] *= 1.0 + 1e-3 + rng.standard_normal() * 1e-9  # ~constant return -> corr denom collapses
            if not present:
                continue
            c = price[s]
            bars.append({"S": f"S{s}", "o": c, "c": c, "h": c * 1.0001, "l": c * 0.9999,
                         "v": 1000.0 + rng.random() * 4000, "t": minute_iso})
        if not bars:  # a real feed always carries >=1 symbol per minute
            c = price[0]
            bars.append({"S": "S0", "o": c, "c": c, "h": c, "l": c, "v": 1000.0, "t": minute_iso})
        out.append(bars)
    return out


def _gappy_corr_breaches(group_name: str) -> bool:
    """Run a group DIRECTLY (engine vs batch, no gate) over a few sparse gappy near-constant-return walks and
    report whether it breaches the production self-check ratio on ANY of them — the shared body for both the
    P2-now-clean groups and the still-load-bearing ones."""
    group = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name == group_name]
    assert group, f"{group_name} missing from registry"
    breached = False
    for seed in (7, 17, 29):  # a few sparse seeds — the breach (where present) is regime-robust across them
        walk = _gappy_corr_minutes(n_sym=12, n_min=28, present_p=0.40, seed=seed)
        ring = MinuteRing(maxlen=120)
        engine: IncrementalEngine | None = None
        for bars in walk:
            if not bars:
                continue
            ring.push(_bars_to_frame(bars))
            frame = ring.materialize()
            ctx = BatchContext(frames={"minute_agg": frame})
            batch = compute_reduction_batch(group, ctx)
            if engine is None:
                engine = IncrementalEngine(group)
            inc = engine.step(frame, slice_derive=True)
            try:
                if _worst_tol_ratio(batch, inc) > _PARITY_BREACH_RATIO:
                    breached = True
            except AssertionError:
                breached = True
    return breached


@pytest.mark.parametrize("group_name", ["return_dynamics", "volume_leads_price"])
def test_gappy_denom_group_now_clean_after_p2_neumaier(group_name: str) -> None:
    """⭐ P2 PROOF (stable summation): on the gappy near-constant-return walk these corr-family groups USED to
    breach engine-vs-batch (their ``incremental_safe=False`` was the gate). The Neumaier-compensated running
    sum (``_comp`` carries the add/expire rounding loss) now makes the corr-kernel power-sum denominator match
    the batch fresh sum, so engine-vs-batch is CLEAN — the former breach is CLOSED. They keep
    ``incremental_safe=False`` until the LEAD sequences the enablement flip; this asserts the parity is now
    green so that flip is unblocked."""
    assert not _gappy_corr_breaches(group_name), (
        f"{group_name} engine-vs-batch is now CLEAN on the gappy walk after the P2 Neumaier fix "
        "(former incremental_safe=False breach closed)"
    )


@pytest.mark.parametrize("group_name", ["price_volume"])
def test_gappy_denom_group_still_breaches_gate_load_bearing(group_name: str) -> None:
    """``price_volume``'s ``incremental_safe=False`` is STILL LOAD-BEARING after P2: its breach on the gappy
    near-constant-return walk is NOT purely a summation-rounding effect (Neumaier closed return_dynamics /
    volume_leads_price but not this one — the cross-product corr-denom straddle here survives stable summation),
    so the gate must stay. Comparing the engine DIRECTLY against the batch (no gate) must still breach."""
    assert _gappy_corr_breaches(group_name), (
        f"{group_name} expected to STILL breach engine-vs-batch on the gappy walk after P2 — its gate remains "
        "load-bearing (stable summation alone does not close this corr-denom straddle)"
    )


def _gappy_market_minutes(n_sym: int, n_min: int, present_p: float, seed: int) -> list[list[dict]]:
    """A gappy walk WITH a dense ``SPY`` market symbol — the regime ``market_beta`` regresses over and the only
    one that exercises its SPY-broadcast corr denominator. SPY prints every minute (so ``_mret`` is non-null);
    the satellite symbols are SPARSE (skip most minutes) and drift at a ~fixed tiny rate, so over a trailing
    window a satellite's few paired (SPY-return, own-return) bars collapse to a near-constant SPY-return x —
    denom_x/denom_y = b·Σx²−(Σx)² becomes a difference of float-noise, and incremental's running sum rounds it
    across the corr defined-guard differently from the batch fresh sum (market_corr/idio_vol null-vs-finite).

    This is the gap the smooth ``_gappy_corr_minutes`` walk could NOT exercise: it carries no SPY symbol, so
    ``market_beta``'s ``_mret`` is all-null and the SPY-regressor denom is never hit — which is exactly why the
    SYNTHETIC sweep cleared market_beta while the real-06-18 A/B (dense SPY + gappy MO/SLB) breached it."""
    rng = np.random.default_rng(seed)
    price = {s: 100.0 + s for s in range(n_sym)}
    spy = 400.0
    out: list[list[dict]] = []
    for mi in range(n_min):
        minute_iso = (BASE + dt.timedelta(minutes=mi)).isoformat()
        spy *= 1.0 + 1e-3 + rng.standard_normal() * 1e-9  # ~constant SPY return -> the broadcast x collapses
        bars: list[dict] = [{"S": "SPY", "o": spy, "c": spy, "h": spy * 1.0001, "l": spy * 0.9999,
                             "v": 1_000_000.0, "t": minute_iso}]
        for s in range(n_sym):
            if not (mi == 0 or rng.random() < present_p):
                continue
            price[s] *= 1.0 + 1e-3 + rng.standard_normal() * 1e-9
            c = price[s]
            bars.append({"S": f"S{s}", "o": c, "c": c, "h": c * 1.0001, "l": c * 0.9999,
                         "v": 1000.0 + rng.random() * 4000, "t": minute_iso})
        out.append(bars)
    return out


def test_market_beta_breaches_on_real_gappy_spy_regressor() -> None:
    """``market_beta``'s ``incremental_safe = False`` is LOAD-BEARING and is the case the SYNTHETIC gappy sweep
    MISSED: feeding a gappy walk WITH a dense SPY regressor through batch vs IncrementalEngine, the SPY-broadcast
    corr denominator collapses on a sparse satellite symbol and the engine emits market_corr=±1 / idio_vol=0
    where batch NULLs — a real null/non-null divergence (reproduced on real 06-18 bars for MO/SLB). The earlier
    smooth ``_gappy_corr_minutes`` walk carried no SPY symbol, so it could not surface this; this test is the
    regression that catches it where the synthetic could not. Comparing the engine DIRECTLY against the batch
    (no gate) must breach, which is exactly WHY the group is routed to batch live."""
    group = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name == "market_beta"]
    assert group, "market_beta missing from registry"
    assert not group[0].incremental_safe, "market_beta must be incremental_safe=False"
    breached = False
    for seed in (7, 17, 29):
        walk = _gappy_market_minutes(n_sym=12, n_min=70, present_p=0.45, seed=seed)
        ring = MinuteRing(maxlen=120)
        engine: IncrementalEngine | None = None
        for bars in walk:
            ring.push(_bars_to_frame(bars))
            frame = ring.materialize()
            ctx = BatchContext(frames={"minute_agg": frame})
            batch = compute_reduction_batch(group, ctx)
            if engine is None:
                engine = IncrementalEngine(group)
            inc = engine.step(frame, slice_derive=True)
            try:
                if _worst_tol_ratio(batch, inc) > _PARITY_BREACH_RATIO:
                    breached = True
            except AssertionError:  # null/non-null mismatch -> the corr-denom straddle
                breached = True
        if breached:
            break
    assert breached, (
        "market_beta expected to breach engine-vs-batch on a gappy walk with a dense SPY regressor "
        "(the SPY-broadcast corr-denom conditioning the incremental_safe=False gate guards against — "
        "the case the smooth no-SPY synthetic sweep missed)"
    )


def test_gappy_denom_groups_stay_on_batch_under_incremental(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """On the gappy near-constant-return walk the unsafe groups stay on the batch path under FP_INCREMENTAL, so
    their output is BYTE-IDENTICAL to the no-flag batch output (frame_equal, not merely within tolerance) — the
    routing the ``incremental_safe = False`` gate produces. (This is the integration counterpart to the raw
    per-group breach above: the gate converts that breach into a no-op by serving the batch recompute.)"""
    walk = _gappy_corr_minutes(n_sym=12, n_min=28, present_p=0.40, seed=17)

    monkeypatch.delenv("FP_INCREMENTAL", raising=False)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    batch = _run(walk, str(tmp_path / "batch"))

    monkeypatch.setenv("FP_INCREMENTAL", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(walk, str(tmp_path / "inc"))

    keys = ["symbol", "minute"]
    for name in INCREMENTAL_UNSAFE:
        assert name in batch, f"{name} expected in reduction output"
        assert batch[name].sort(keys).equals(inc[name].sort(keys)), \
            f"{name} must be byte-identical to batch under FP_INCREMENTAL (it stays on the batch path)"


def test_incremental_parity_helper() -> None:
    """``_incremental_parity`` returns 0 for identical frames, a large tolerance-multiple for a perturbed
    one, and inf for a missing group or a null/non-null mismatch."""
    a = pl.DataFrame({"symbol": ["A", "B"], "minute": [BASE, BASE], "f": [1.0, 2.0]})
    assert _incremental_parity({"g": a}, {"g": a}) == 0.0
    perturbed = {"g": a.with_columns((pl.col("f") + 1.0).alias("f"))}
    assert _incremental_parity({"g": a}, perturbed) > _PARITY_BREACH_RATIO
    assert _incremental_parity({"g": a}, {}) == float("inf")  # missing group
    nulled = {"g": a.with_columns(pl.lit(None, dtype=pl.Float64).alias("f"))}
    assert _incremental_parity({"g": a}, nulled) == float("inf")  # null vs non-null


def test_unsafe_groups_are_flagged() -> None:
    """The conditioning-sensitive reduction groups carry ``incremental_safe = False`` and every other
    reduction group is safe — so the per-group split (incremental for safe, batch for unsafe) is driven by a
    declared attribute, not a hard-coded name list in the dispatch."""
    reduction = {g.name: g for g in REGISTRY.groups() if isinstance(g, ReductionGroup)}
    unsafe = {name for name, group in reduction.items() if not group.incremental_safe}
    assert unsafe == INCREMENTAL_UNSAFE, f"unexpected incremental_safe set: {unsafe}"


def test_unsafe_group_stays_on_batch_under_incremental(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """On a SMOOTH (near-perfect-fit) walk — the regime where the incremental running sums diverge from the
    batch fresh sums beyond tolerance — the unsafe groups (volume / price_volume) stay on the batch path under
    FP_INCREMENTAL, so their output is BYTE-IDENTICAL to the no-flag batch output (not merely within tolerance).
    Meanwhile the safe groups still ride the incremental path. This proves the split protects the sensitive
    features from the conditioning corner while still serving everything else fast."""
    smooth = _stream_minutes(n_sym=8, n_min=25, present_p=0.7, seed=3, vol=0.002)  # near-linear -> near-perfect fit

    monkeypatch.delenv("FP_INCREMENTAL", raising=False)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    batch = _run(smooth, str(tmp_path / "batch"))

    monkeypatch.setenv("FP_INCREMENTAL", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(smooth, str(tmp_path / "inc"))

    keys = ["symbol", "minute"]
    for name in INCREMENTAL_UNSAFE:
        assert name in batch, f"{name} expected in reduction output"
        # Byte-identical: the unsafe group ran the SAME batch recompute in both runs (frame_equal, not a tol).
        assert batch[name].sort(keys).equals(inc[name].sort(keys)), \
            f"{name} must be byte-identical to batch under FP_INCREMENTAL (it stays on the batch path)"


def test_incremental_capture_no_breach_when_unsafe_gated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """With the unsafe groups gated to batch, the FP_INCREMENTAL output matches the batch output within the
    benign-drift breach ratio for EVERY group, even on the smooth near-perfect-fit walk that previously drove
    the volume/price_volume conditioning divergence past tolerance (test_ols_near_perfect_fit_is_flagged)."""
    smooth = _stream_minutes(n_sym=8, n_min=25, present_p=0.7, seed=3, vol=0.002)

    monkeypatch.delenv("FP_INCREMENTAL", raising=False)
    batch = _run(smooth, str(tmp_path / "batch"))

    monkeypatch.setenv("FP_INCREMENTAL", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(smooth, str(tmp_path / "inc"))

    worst = _worst_tol_ratio(batch, inc)
    assert worst < _PARITY_BREACH_RATIO, f"gated incremental still breached: worst {worst}x tolerance"
