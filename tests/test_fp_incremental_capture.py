"""Incremental fast path wired into ``capture.process_bars`` == the batch path, cell-for-cell.

This is the live-integration parity gate for P1 #1 (per-symbol fast path). ``process_bars`` is the shared
compute core for the mock, real, and sharded capture clients; the ``incremental_safe`` ``ReductionGroup``s
are ALWAYS assembled from per-bucket ``IncrementalEngine`` running sums (via ``step`` — the SAME
``assemble_from_long`` the batch uses, so warmup/flag null handling is byte-identical) instead of
recomputing the whole buffer each minute. This is the DEFAULT path now (no master env switch). Parity is
sacred (CLAUDE.md): the incremental output must equal the batch output within tolerance, under a FLUCTUATING
active symbol set (the live regime).

The ``incremental_safe=False`` groups stay on the batch fresh-sum recompute; ``_run_all_batch`` (force every
group to batch) provides the pure-batch baseline these tests compare the default incremental output against.

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

from quantlib.features import capture, declarative
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
from quantlib.features.reduction_anchor import (
    _RTH_MINUTES_PER_DAY,
    attach_reduction_anchors,
    attach_volume_anchor,
)
from quantlib.features.registry import REGISTRY

BASE = dt.datetime(2026, 6, 16, 14, 0, tzinfo=dt.timezone.utc)


def _with_anchors(frame: pl.DataFrame) -> pl.DataFrame:
    """Attach BOTH the volume and close per-symbol centering anchors (production attaches them via
    attach_reduction_anchors before either path runs), so the anchor-declaring OLS-y-centered groups
    (trend_quality, clean_momentum) are runnable / select their anchor column on a synthetic minute frame.
    Value-additive — the close anchor is consumed only under FP_RUST_REDUCE; it never changes a feature value
    with the flag off, it only makes the group selectable.

    The ``daily`` snapshot uses each symbol's EARLIEST buffered close/volume, NOT ``last()``. The anchor is a
    per-SESSION CONSTANT (production sources it from the prior-day daily bar, fixed all session) — the parity-
    critical contract reduction_anchor.py guarantees: the SAME per-symbol value read identically by the batch
    fresh-sum path AND the per-minute incremental fold. ``last()`` over the GROWING per-minute buffer made the
    2-sig-fig anchor FLIP across minutes as a symbol's latest close drifted over a sig-fig boundary, so the
    incremental engine (folds each minute once at the then-current anchor) and the batch (re-derives every row
    at the current anchor) centered the SAME historical row on DIFFERENT constants — a synthetic divergence the
    production fixed-snapshot anchor never has (verified: late-appearance/degenerate FP_RUST_REDUCE walks go
    from ~9e5x to <1e-4x once the anchor is session-constant). ``first()`` over the cumulative buffer is the
    stable per-session proxy."""
    daily = (
        frame.sort(["symbol", "minute"])
        .group_by("symbol", maintain_order=True)
        .agg(
            (pl.col("volume").first() * _RTH_MINUTES_PER_DAY).alias("volume"),
            pl.col("close").first().alias("close"),
        )
        .with_columns(pl.lit(1).alias("date"))
    )
    return attach_reduction_anchors({"minute_agg": frame, "daily": daily})["minute_agg"]


# The conditioning-sensitive groups gated to the batch fresh-sum recompute, each DEMONSTRATED to breach the
# engine-vs-batch parity self-check on a gappy/near-flat walk by test_gappy_denom_group_breaches_raw_so_gate_is_
# load_bearing below (worst ratio + null/non-null mismatch recorded there). ``volume`` (variance-family std:
# power-sum sqrt vs backfill rolling_std_by FORMULA gap) is the original. ``price_volume`` reverts to gated
# (was #155 True): its ``pv_correlation`` regresses return on RAW share volume and breaches BEYOND the b==2
# perfect-fit corner the n==2 guard closed (77x tolerance + 6 null mismatches in the sweep).
#
# UN-GATED by the shared-engine rebase_time_axis comp-realization fix (this PR): ``return_dynamics`` (lagged-
# return autocorrelation) and ``market_beta`` (each symbol's return regressed on SPY's broadcast return). Both
# breached ONLY in the shared engine under FP_RUST_REDUCE, where a co-resident time-OLS group (price_volume.obv)
# realized the Neumaier compensation over the WHOLE running array, collapsing an unanchored corr group's
# Σxx-exactly-zero flat-name cell into a ~1e-22 residue that tripped the corr defined-guard. Scoping the comp-
# realization to only the time-OLS columns the rebase shifts leaves every other column's running/_comp untouched,
# so the real-06-17 shared-engine A/B now shows 0 null-flips on price_volume/return_dynamics/market_beta.
#
# NOT gated (verified parity-clean on the real-06-18 gappy A/B, no null mismatch, worst < 4e-4x tolerance):
# distribution, volatility — their power-sum-moment / std algebra does NOT collapse the way the correlation-of-
# two-sparse-series groups do.
INCREMENTAL_UNSAFE = {
    # price_volume's incremental_safe is a PROPERTY gated on FP_RUST_REDUCE (its volume-y anchor + obv arm
    # together). With the flag OFF (the default this set is evaluated under) it is False, so it belongs here.
    "price_volume",
    # residual_analysis: the OLS residual SSR is a difference of large near-equal centered power sums on a
    # near-perfect intraday fit — the corr-denom-class centering (sibling), not the std-class. Stays gated here.
    "residual_analysis",
    # The REAL-DATA soak NO-GO breachers (docs/INCREMENTAL_READINESS.md, 2026-06-17 A/B, set False by #332):
    # rare guard-straddle / power-sum-cancellation cells the synthetic degenerate stream cannot reach on real
    # gappy tape. Batch-gated until the cancellation-free reduction-denom fix lands; same class as the trio.
    # (``distribution`` was previously here — its higher-moment Σ(r)^4 cancellation is now UN-GATED by the
    # return-anchor centering + the raised moment defined-guard; see quantlib/features/groups/distribution.py.)
    # (``return_dynamics`` was previously here — its autocorrelation denom null-flip was a co-resident-time-OLS
    # perturbation under FP_RUST_REDUCE, now UN-GATED by the scoped rebase_time_axis comp-realization fix; see
    # quantlib/features/incremental.py.)
    "range_expansion",  # ratio-denom `>0` guard straddle (7.8% of minutes, the most frequent)
    "trend_quality",  # OLS R² cov²/(var·var) denom straddle (2.7%)
    "clean_momentum",  # moment/std power-sum cancellation (1.5%)
}
# The groups #332 re-gated to batch on the real-data soak verdict — they must be incremental_safe=False and
# therefore byte-identical to batch under FP_INCREMENTAL. (return_dynamics left this set when the shared-engine
# rebase fix closed its only remaining breach — the co-resident-time-OLS perturbation under FP_RUST_REDUCE.)
INCREMENTAL_REGATED = {
    "range_expansion",
    "trend_quality",
    "clean_momentum",
}


def _stream_minutes(
    n_sym: int, n_min: int, present_p: float, seed: int, vol: float = 0.02
) -> list[list[dict]]:
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
            bars.append(
                {
                    "S": f"S{s}",
                    "o": c * 0.999,
                    "c": c,
                    "h": c * 1.002,
                    "l": c * 0.998,
                    "v": 1000.0 + rng.random() * 4000,
                    "t": minute_iso,
                }
            )
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
            bars.append(
                {
                    "S": f"S{s}",
                    "o": c * 0.999,
                    "c": c,
                    "h": c * 1.002,
                    "l": c * 0.998,
                    "v": v,
                    "t": minute_iso,
                }
            )
        out.append(bars)
    return out


def _run(stream: list[list[dict]], root: str) -> dict[str, pl.DataFrame]:
    """Drive a stream through ``process_bars`` (accumulate in RAM, no store) and return the per-group output.
    The ``incremental_safe`` groups ride the incremental running sums (the default path); the unsafe groups
    recompute from the batch fresh sums."""
    state = CaptureState()
    for bars in stream:
        process_bars(state, bars, root, "mock", "2026-06-16", 120, accumulate=True, write=False)
    return state.accumulated


def _run_all_batch(stream: list[list[dict]], root: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, pl.DataFrame]:
    """A pure-BATCH baseline: force EVERY reduction group onto the batch fresh-sum recompute (set
    ``incremental_safe=False`` on all of them for this run) so ``process_bars`` never touches the incremental
    engine. The reference the default incremental output is compared against — now that incremental is the
    default, the batch path is no longer reachable via an env switch."""
    # Force EVERY reduction group onto batch. ``price_volume.incremental_safe`` is a flag-gated PROPERTY (True
    # only under FP_RUST_REDUCE), so it cannot be shadowed by an instance ``setattr`` — patch its CLASS to a
    # plain False, and clear the flag so the property (and the y-anchor) stay in their parked/default state.
    monkeypatch.setattr(declarative, "_USE_RUST_REDUCE", False)
    for group in REGISTRY.groups():
        if isinstance(group, ReductionGroup):
            if isinstance(type(group).__dict__.get("incremental_safe"), property):
                monkeypatch.setattr(type(group), "incremental_safe", False)
            else:
                monkeypatch.setattr(group, "incremental_safe", False)
    state = CaptureState()
    for bars in stream:
        process_bars(state, bars, root, "mock", "2026-06-16", 120, accumulate=True, write=False)
    return state.accumulated


def _worst_tol_ratio(batch: dict, inc: dict, *, cols_drop: tuple[str, ...] = ()) -> float:
    """Worst divergence between two accumulated per-group dicts, as a multiple of the parity tolerance,
    joined on (symbol, minute). ``cols_drop`` excludes named feature columns (used to isolate the r2 family).
    """
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


def test_default_config_is_monitoring_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no env set, ``_incremental_config() == (parity_check=False, slice_derive=True)``. The incremental
    fast path itself is NOT gated by an env switch anymore — the ``incremental_safe`` groups always ride the
    running sums (the value-identical default). ``FP_INCREMENTAL_PARITY`` (monitoring-only self-check) is OFF
    by default; ``slice_derive`` is ON by default (the verified-clean per-symbol-tail derive — its one-shot
    seed equals the minute fold, so warm-start is value-identical). ``FP_INCREMENTAL_SLICE=0`` opts out."""
    for var in ("FP_INCREMENTAL_PARITY", "FP_INCREMENTAL_SLICE"):
        monkeypatch.delenv(var, raising=False)
    assert _incremental_config() == (False, True)
    monkeypatch.setenv("FP_INCREMENTAL_SLICE", "0")
    assert _incremental_config() == (False, False)  # explicit opt-out to the whole-buffer derive


def test_incremental_capture_matches_batch(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The DEFAULT incremental path (gap-safe whole-buffer derive, no env set) produces the SAME per-group
    output as a forced-batch baseline on well-conditioned data, under a fluctuating active symbol set across
    many minutes — within a small multiple of the parity tolerance (benign float drift)."""
    stream = _stream_minutes(n_sym=8, n_min=50, present_p=0.7, seed=3)

    with monkeypatch.context() as forced_batch:
        batch = _run_all_batch(stream, str(tmp_path / "batch"), forced_batch)

    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)  # gap-safe whole-buffer derive (open slice constraint)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(stream, str(tmp_path / "inc"))  # default: incremental_safe groups ride the running sums

    assert batch, "expected reduction-group output"
    worst = _worst_tol_ratio(batch, inc)
    assert worst < _PARITY_BREACH_RATIO, f"incremental diverged from batch: worst {worst}x tolerance"


def test_parity_selfcheck_records_clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """FP_INCREMENTAL_PARITY=1 (monitoring-only) runs BOTH paths each minute, writes the incremental truth, and
    records a within-drift divergence (no breach) on well-conditioned data. Exercises the self-check wiring."""
    stream = _stream_minutes(n_sym=6, n_min=40, present_p=0.7, seed=9)
    seen: list[tuple[str, float, bool]] = []
    monkeypatch.setattr(capture.metrics, "record_incremental_parity",
                        lambda ri, r, b: seen.append((ri, r, b)))
    monkeypatch.setenv("FP_INCREMENTAL_PARITY", "1")
    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)

    _run(stream, str(tmp_path / "selfcheck"))

    assert seen, "self-check should record a parity sample each minute"
    assert not any(
        breached for _, _, breached in seen
    ), f"no minute should breach on well-conditioned data; worst {max(r for _, r, _ in seen)}x tolerance"


def test_regated_groups_are_incremental_unsafe() -> None:
    """The 5 real-data-soak NO-GO groups carry ``incremental_safe = False`` (re-gated to batch by #332), so
    they ride the batch path under FP_INCREMENTAL — proven byte-identical to batch by
    ``test_unsafe_group_stays_on_batch_under_incremental`` (which iterates the whole INCREMENTAL_UNSAFE set).
    """
    regated = {
        g.name for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name in INCREMENTAL_REGATED
    }
    assert regated == INCREMENTAL_REGATED, "re-gated soak groups missing from registry"
    assert INCREMENTAL_REGATED <= INCREMENTAL_UNSAFE, "re-gated groups must be in the unsafe set"
    for group in REGISTRY.groups():
        if isinstance(group, ReductionGroup) and group.name in INCREMENTAL_REGATED:
            assert not group.incremental_safe, f"{group.name} must be incremental_safe=False (soak NO-GO)"


@pytest.mark.parametrize("slice_derive", [True, False])
def test_ols_near_perfect_fit_engine_matches_batch(slice_derive: bool) -> None:
    """ENGINE-KERNEL correctness (independent of the prod gating decision): on a SMOOTH (near-linear) price
    walk the OLS R²/correlation family fits near-perfectly (R²→1, the b==2 corner is exact) — the historical
    breach source. With the time-OLS origin-rebase (PR #132) and the n==2 perfect-fit guard
    (_OLS_PERFECT_FIT_COUNT: r2=1.0 / corr=sign(cov) at b==2), the IncrementalEngine agrees with the batch on
    the time-axis-OLS groups on BOTH the live slice-derive and the whole-buffer paths. These groups are
    PROD-GATED to batch by the real-data soak (rare gappy-tape straddles this smooth walk does not hit), but
    the kernel itself stays well-conditioned on the smooth regime — the regression guard for the OLS rebase.
    """
    smooth = _stream_minutes(
        n_sym=8, n_min=20, present_p=0.7, seed=3, vol=0.002
    )  # near-linear -> near-perfect fit
    groups = [
        g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name in INCREMENTAL_REGATED
    ]
    assert {g.name for g in groups} == INCREMENTAL_REGATED, "re-gated OLS groups missing from registry"
    ring = MinuteRing(maxlen=120)
    engine: IncrementalEngine | None = None
    worst = 0.0
    for bars in smooth:
        if not bars:
            continue
        ring.push(_bars_to_frame(bars))
        frame = _with_anchors(ring.materialize())
        ctx = BatchContext(frames={"minute_agg": frame})
        batch = compute_reduction_batch(groups, ctx)
        if engine is None:
            engine = IncrementalEngine(groups)
        inc = engine.step(frame, slice_derive=slice_derive)
        worst = max(worst, _worst_tol_ratio(batch, inc))
    assert (
        worst < _PARITY_BREACH_RATIO
    ), f"re-gated OLS engine must match batch on perfect-fit walk: {worst}x"


@pytest.mark.parametrize(
    "seed,present_p,vol",
    [
        (5, 0.30, 0.0002),  # very sparse + ultra-smooth: many b==2 perfect-fit windows
        (11, 0.45, 0.0005),  # b==2-heavy churn
        (23, 0.35, 0.0003),  # sparse near-linear
        (7, 0.70, 0.020),  # well-conditioned (control)
    ],
)
def test_regated_ols_groups_engine_matches_batch_on_degenerate_walks(
    seed: int, present_p: float, vol: float
) -> None:
    """ENGINE-KERNEL correctness across degenerate regimes (n==2 perfect-fit corners from sparse presence,
    near-flat ultra-smooth walks) AND a well-conditioned control — on the LIVE slice-derive path. The n==2
    perfect-fit guard + origin-rebase keep the OLS r2/corr family engine-vs-batch parity-true on THESE
    synthetic regimes (no breach, no null/non-null mismatch). The groups are prod-gated to batch by the
    real-data soak for the GAPPIER regimes these synthetic walks do not reproduce — so this is the kernel
    regression guard, not a flip claim."""
    walk = _stream_minutes(n_sym=10, n_min=18, present_p=present_p, seed=seed, vol=vol)
    flipped = [
        g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name in INCREMENTAL_REGATED
    ]
    ring = MinuteRing(maxlen=120)
    engine: IncrementalEngine | None = None
    worst = 0.0
    for bars in walk:
        if not bars:
            continue
        ring.push(_bars_to_frame(bars))
        frame = _with_anchors(ring.materialize())
        ctx = BatchContext(frames={"minute_agg": frame})
        batch = compute_reduction_batch(flipped, ctx)
        if engine is None:
            engine = IncrementalEngine(flipped)
        inc = engine.step(frame, slice_derive=True)
        worst = max(worst, _worst_tol_ratio(batch, inc))
    assert worst < _PARITY_BREACH_RATIO, f"flipped OLS breached on degenerate walk (seed={seed}): {worst}x"


def _late_appearance_minutes(
    n_sym: int, n_min: int, present_p: float, seed: int, vol: float
) -> list[list[dict]]:
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
            bars.append(
                {
                    "S": f"S{s}",
                    "o": c * 0.999,
                    "c": c,
                    "h": c * 1.002,
                    "l": c * 0.998,
                    "v": 1000.0 + rng.random() * 4000,
                    "t": minute_iso,
                }
            )
        if not bars:  # a real feed always carries >=1 symbol per minute
            s = int(rng.integers(n_sym))
            c = price[s]
            bars.append(
                {
                    "S": f"S{s}",
                    "o": c * 0.999,
                    "c": c,
                    "h": c * 1.002,
                    "l": c * 0.998,
                    "v": 1000.0 + rng.random() * 4000,
                    "t": minute_iso,
                }
            )
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
def test_regated_ols_engine_matches_batch_on_late_appearance_reseed(
    seed: int, present_p: float, vol: float
) -> None:
    """ENGINE-KERNEL regression guard: cell-for-cell batch==incremental on the time-axis-OLS groups when
    symbols FIRST APPEAR at later minutes (no clean minute-0 warmup), so each new ticker triggers a
    ``SymbolSetExpanded`` re-seed that folds the whole buffer through the slice-derive path for HISTORICAL
    minutes. Guards the rust slice-derive future-row lag leak: without the ``<= minute`` point-in-time cut a
    first-appearance return's prior-close lag came back non-null, double-counting the OLS pairing ``b`` and
    emitting a ±1.0 correlation (the n==2 perfect-fit value) where the batch correctly emits ``null``. The
    helper ``_worst_tol_ratio`` returns ``inf`` on any null/non-null mismatch, so a re-breach fails loudly.
    (These groups are prod-gated to batch by the real-data soak; this test exercises the engine kernel
    directly so the slice-derive correctness stays guarded regardless of the gating decision.)

    Under FP_RUST_REDUCE this ALSO guards the y-anchor centering contract: ``_with_anchors`` now sources the
    close anchor from each symbol's session-constant EARLIEST close (a non-flipping per-session constant), so
    the engine (folds each minute once at the fold-time anchor) and the batch (re-derives every row at the
    current anchor) center the SAME historical row on the SAME constant. (A ``last()``-over-the-growing-buffer
    anchor flipped across the 2-sig-fig boundary as a symbol's latest close drifted and produced a SYNTHETIC
    ~9e5x divergence that the production fixed-snapshot anchor never has.)"""
    walk = _late_appearance_minutes(n_sym=20, n_min=40, present_p=present_p, seed=seed, vol=vol)
    flipped = [
        g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name in INCREMENTAL_REGATED
    ]
    ring = MinuteRing(maxlen=120)
    engine: IncrementalEngine | None = None
    worst = 0.0
    for bars in walk:
        if not bars:
            continue
        ring.push(_bars_to_frame(bars))
        frame = _with_anchors(ring.materialize())
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


def _with_volume_anchor(frame: pl.DataFrame) -> pl.DataFrame:
    """Attach volume's per-symbol centering anchor to a test minute frame — the production capture/backfill
    attaches it where minute_agg is built. Production sources the anchor from the daily-BAR total volume and
    divides by the session-minute count to reach the per-minute scale (reduction_anchor._RTH_MINUTES_PER_DAY);
    so here we synthesize a daily snapshot at DAILY-TOTAL scale (the per-symbol per-minute level x the session
    minutes) and let ``attach_volume_anchor`` re-derive the per-minute anchor exactly as it does in prod."""
    daily = (
        frame.group_by("symbol", maintain_order=True)
        .agg((pl.col("volume").last() * _RTH_MINUTES_PER_DAY).alias("volume"))
        .with_columns(pl.lit(1).alias("date"))
    )
    return attach_volume_anchor(frame, daily)


def test_volume_centered_std_is_clean_after_centering() -> None:
    """⭐ GATE 3 (the un-gate proof): on the INTERMEDIATE-variance huge-volume window that USED to breach
    (volume_zscore ~3e-5 apart, rel ~15x — the batch-vs-canonical std FORMULA gap), volume's CENTERED
    power-sum std (Σ(v−a)/Σ(v−a)² on the per-symbol anchor) makes engine-vs-batch CLEAN. This INVERTS the
    former gate-is-load-bearing assertion — the proof the gate can drop and ``incremental_safe = True`` holds.
    The anchor is attached to the frame BOTH paths fold (the same source), so they center identically."""
    walk = _flat_volume_minutes(n_sym=8, n_min=25, present_p=0.7, seed=9, vol=0.01, vol_noise=1e-5)
    volume = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name == "volume"]
    ring = MinuteRing(maxlen=120)
    engine: IncrementalEngine | None = None
    breached = False
    for bars in walk:
        if not bars:
            continue
        ring.push(_bars_to_frame(bars))
        frame = _with_volume_anchor(ring.materialize())
        ctx = BatchContext(frames={"minute_agg": frame})
        batch = compute_reduction_batch(volume, ctx)
        if engine is None:
            engine = IncrementalEngine(volume)
        inc = engine.step(frame, slice_derive=True)
        try:
            if _worst_tol_ratio(batch, inc) > _PARITY_BREACH_RATIO:
                breached = True
        except AssertionError:
            breached = True
    assert not breached, (
        "volume engine-vs-batch must be CLEAN after the centered-power-sum std — the formula-gap breach is "
        "closed, so incremental_safe=True is correct (gate 3, the un-gate proof)."
    )


def _gappy_corr_minutes(n_sym: int, n_min: int, present_p: float, seed: int) -> list[list[dict]]:
    """A GAPPY, near-constant-return stream: symbols skip most minutes (sparse presence) and the price drifts at
    a ~fixed tiny rate, so a trailing window holds only a few bars whose one-minute returns are ~equal. This is
    the conditioning case the 19 smooth-synthetic walks miss: the correlation kernel's paired series (a return
    against its own lag / against lagged volume) collapse to ~constant over the sparse window, so the corr
    denominator √(var_x·var_y) is a difference of near-equal running sums — the incremental running sums round
    it across the corr defined-guard differently from the batch fresh sum, so incremental emits a value where
    batch emits NULL (or disagrees past the breach ratio). Re-paired counts under sparse presence amplify it.
    """
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
            bars.append(
                {
                    "S": f"S{s}",
                    "o": c,
                    "c": c,
                    "h": c * 1.0001,
                    "l": c * 0.9999,
                    "v": 1000.0 + rng.random() * 4000,
                    "t": minute_iso,
                }
            )
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
            # price_volume's InputSpec declares __anchor_volume (its pv_correlation x-side conditioning), so the
            # materialized frame must carry the volume anchor production attaches before either path runs.
            frame = _with_volume_anchor(ring.materialize())
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
    """⭐ On the gappy near-constant-return walk these corr-family groups USED to breach engine-vs-batch (their
    ``incremental_safe=False`` was the gate). The Neumaier-compensated running sum (``_comp`` carries the
    add/expire rounding loss) makes the corr-kernel power-sum denominator match the batch fresh sum, so
    engine-vs-batch is CLEAN — the former breach is CLOSED. Both are now ``incremental_safe=True`` (volume_leads_
    price was already armed; return_dynamics un-gated once the shared-engine rebase fix closed its co-resident-
    time-OLS perturbation); this asserts the gappy-walk parity stays green."""
    assert not _gappy_corr_breaches(group_name), (
        f"{group_name} engine-vs-batch is now CLEAN on the gappy walk after the P2 Neumaier fix "
        "(former incremental_safe=False breach closed)"
    )


def test_price_volume_xside_clean_on_gappy_walk() -> None:
    """``price_volume``'s breach on the gappy NEAR-CONSTANT-RETURN walk (the x-side: pv_correlation regresses a
    ~constant return whose ``denom_x = b·Σr² − (Σr)²`` cancels at the ~1e-12 relative level) is now CLOSED by the
    translation-invariant x-side variance guard (``_OLS_DENOM_X_CENTERED_REL_EPS``) — the return-side analogue of
    the y-side fix. This walk carries VARYING volume (y-side well-conditioned), so it isolated the x-side, and the
    engine-vs-batch comparison is now CLEAN. ``price_volume`` nonetheless stays ``incremental_safe=False`` with
    FP_RUST_REDUCE OFF (the raw-volume Y-SIDE denom is then still uncentered — a separate gappy regime breaches
    it), and flips to True only under FP_RUST_REDUCE (both sides conditioned) — see
    ``test_incremental_safe_gated_on_rust_reduce`` and ``test_fp_price_volume_xside``."""
    assert not _gappy_corr_breaches("price_volume"), (
        "price_volume engine-vs-batch is now CLEAN on the gappy near-constant-RETURN walk — the x-side "
        "denom_x cancellation is closed by the centered-variance x guard (the y-side stays the gate's reason)"
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
        bars: list[dict] = [
            {
                "S": "SPY",
                "o": spy,
                "c": spy,
                "h": spy * 1.0001,
                "l": spy * 0.9999,
                "v": 1_000_000.0,
                "t": minute_iso,
            }
        ]
        for s in range(n_sym):
            if not (mi == 0 or rng.random() < present_p):
                continue
            price[s] *= 1.0 + 1e-3 + rng.standard_normal() * 1e-9
            c = price[s]
            bars.append(
                {
                    "S": f"S{s}",
                    "o": c,
                    "c": c,
                    "h": c * 1.0001,
                    "l": c * 0.9999,
                    "v": 1000.0 + rng.random() * 4000,
                    "t": minute_iso,
                }
            )
        out.append(bars)
    return out


def test_market_beta_xside_clean_on_gappy_spy_regressor() -> None:
    """``market_beta`` regresses each ticker's return on SPY's broadcast return — so on a gappy walk with a dense
    NEAR-CONSTANT-RETURN SPY, the X regressor (the SPY return) collapses and ``denom_x = b·Σx² − (Σx)²`` cancels,
    which used to make the engine emit market_corr=±1 / idio_vol=0 where batch NULLs (the real-06-18 MO/SLB
    breach the smooth no-SPY synthetic sweep missed). The SAME shared translation-invariant x-side variance guard
    (``_OLS_DENOM_X_CENTERED_REL_EPS``) that closes price_volume's return x-side ALSO closes this — a near-
    constant SPY-return window is now NULL on BOTH paths, so the engine-vs-batch comparison is CLEAN (a verified
    cross-group benefit of the shared guard). market_beta is now ``incremental_safe = True`` (un-gated): the
    x-side breach class is closed by this guard, and its only remaining real-tape breach — the shared-engine
    rebase perturbation when price_volume's obv time regression co-resides — is fixed in
    ``WindowedSumState.rebase_time_axis`` (real-soak 2026-06-17 CLEAN at FR=0 and FR=1).
    """
    group = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name == "market_beta"]
    assert group, "market_beta missing from registry"
    assert group[0].incremental_safe, "market_beta is now incremental_safe=True (x-side guard + rebase fix)"
    breached = False
    for seed in (7, 17, 29):
        walk = _gappy_market_minutes(n_sym=12, n_min=70, present_p=0.45, seed=seed)
        ring = MinuteRing(maxlen=120)
        engine: IncrementalEngine | None = None
        for bars in walk:
            ring.push(_bars_to_frame(bars))
            # price_volume's InputSpec declares __anchor_volume (its pv_correlation x-side conditioning), so the
            # materialized frame must carry the volume anchor production attaches before either path runs.
            frame = _with_volume_anchor(ring.materialize())
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
    assert not breached, (
        "market_beta engine-vs-batch must now be CLEAN on the gappy near-constant-SPY-return walk — the "
        "SPY-return x-side denom_x cancellation is closed by the shared centered-variance x guard "
        "(_OLS_DENOM_X_CENTERED_REL_EPS), the same fix that arms price_volume"
    )


def test_gappy_denom_groups_stay_on_batch_under_incremental(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """On the gappy near-constant-return walk the unsafe groups stay on the batch path under FP_INCREMENTAL, so
    their output is BYTE-IDENTICAL to the no-flag batch output (frame_equal, not merely within tolerance) — the
    routing the ``incremental_safe = False`` gate produces. (This is the integration counterpart to the raw
    per-group breach above: the gate converts that breach into a no-op by serving the batch recompute.)"""
    walk = _gappy_corr_minutes(n_sym=12, n_min=28, present_p=0.40, seed=17)

    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    with monkeypatch.context() as forced_batch:
        batch = _run_all_batch(walk, str(tmp_path / "batch"), forced_batch)

    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(walk, str(tmp_path / "inc"))  # default: safe groups incremental, unsafe stay batch

    keys = ["symbol", "minute"]
    for name in INCREMENTAL_UNSAFE:
        assert name in batch, f"{name} expected in reduction output"
        assert batch[name].sort(keys).equals(inc[name].sort(keys)), \
            f"{name} must be byte-identical to the batch baseline (it stays on the batch path by its gate)"


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
    smooth = _stream_minutes(
        n_sym=8, n_min=25, present_p=0.7, seed=3, vol=0.002
    )  # near-linear -> near-perfect fit

    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    with monkeypatch.context() as forced_batch:
        batch = _run_all_batch(smooth, str(tmp_path / "batch"), forced_batch)

    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(smooth, str(tmp_path / "inc"))  # default: safe groups incremental, unsafe stay batch

    keys = ["symbol", "minute"]
    for name in INCREMENTAL_UNSAFE:
        assert name in batch, f"{name} expected in reduction output"
        # Byte-identical: the unsafe group ran the SAME batch recompute in both runs (frame_equal, not a tol).
        assert batch[name].sort(keys).equals(inc[name].sort(keys)), \
            f"{name} must be byte-identical to the batch baseline (it stays on the batch path by its gate)"


def test_incremental_capture_no_breach_when_unsafe_gated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """With the unsafe groups gated to batch, the DEFAULT incremental output matches a forced-batch baseline
    within the benign-drift breach ratio for EVERY group, even on the smooth near-perfect-fit walk that
    previously drove the volume/price_volume conditioning divergence past tolerance."""
    smooth = _stream_minutes(n_sym=8, n_min=25, present_p=0.7, seed=3, vol=0.002)

    with monkeypatch.context() as forced_batch:
        batch = _run_all_batch(smooth, str(tmp_path / "batch"), forced_batch)

    monkeypatch.delenv("FP_INCREMENTAL_SLICE", raising=False)
    monkeypatch.delenv("FP_INCREMENTAL_PARITY", raising=False)
    inc = _run(smooth, str(tmp_path / "inc"))  # default: safe groups incremental, unsafe stay batch

    worst = _worst_tol_ratio(batch, inc)
    assert worst < _PARITY_BREACH_RATIO, f"gated incremental still breached: worst {worst}x tolerance"
