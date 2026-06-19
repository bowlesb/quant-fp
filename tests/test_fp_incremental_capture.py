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

# Only ``volume`` remains gated: its variance-family std (sqrt(Σv²−(Σv)²/n) vs backfill rolling_std_by) is a
# batch-vs-canonical FORMULA gap (Lead-owned centered-std batch change). The OLS-corner groups (price_volume's
# pv_correlation, trend_quality/clean_momentum's r2) are now parity-true via the n==2 perfect-fit guard
# (_OLS_PERFECT_FIT_COUNT) + the time-OLS origin-rebase, so they ride the incremental fast path.
INCREMENTAL_UNSAFE = {"volume"}
# n==2-guard-fixed (now incremental_safe): they ride the incremental fast path.
INCREMENTAL_FLIPPED = {"price_volume", "trend_quality", "clean_momentum"}


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


def _flat_volume_minutes(n_sym: int, n_min: int, present_p: float, seed: int, vol: float) -> list[list[dict]]:
    """A stream with NEAR-CONSTANT huge share volume (the worst variance-cancellation regime for volume_zscore:
    Σv² and (Σv)²/n are large near-equal sums whose difference is float noise, so the power-sum std flips
    across the relative null-floor differently from backfill's rolling_std_by). Price still drifts normally."""
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
            v = max(5_000_000.0 + rng.standard_normal(), 1.0)  # huge, ~constant: std cancellation stress
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
    flipped OLS groups (trend_quality, clean_momentum, price_volume's pv_correlation) — well under the breach
    ratio on BOTH the live slice-derive and the whole-buffer paths. This is the parity proof gating their
    ``incremental_safe = True`` flip."""
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


def test_volume_still_gated_breaches_on_degenerate_variance() -> None:
    """``volume`` stays gated: its z-score std (power-sum sqrt(Σv²−(Σv)²/n) on the live/incremental path vs
    backfill rolling_std_by) diverges on a near-constant-volume window — a real batch-vs-canonical FORMULA gap
    (null/non-null at the std floor, or a large z-score disagreement at n=2/3). This asserts the gate is still
    LOAD-BEARING for volume so its ``incremental_safe = False`` is not stale; flipping it needs the centered-std
    batch change. Comparing the engine DIRECTLY against the batch (no gate) must breach."""
    walk = _flat_volume_minutes(n_sym=8, n_min=25, present_p=0.7, seed=9, vol=0.01)
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
    assert breached, "volume variance-family breach expected (gate is load-bearing)"


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
