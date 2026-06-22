"""The pv_correlation RETURN x-side (denom_x) cancellation — the OPEN hazard that gated ``price_volume`` from
``incremental_safe`` even after #402 closed the volume y-side.

pv_correlation is a Pearson corr of one-minute RETURN (x, ~1e-4) against share VOLUME (y). #402 conditioned the
Y side (raw volume ~1e6) via the per-symbol ``__anchor_volume`` y-centering. But the X side has its OWN
cancellation: on a near-CONSTANT-but-nonzero return (a flat/illiquid name grinding one direction by the same
tiny amount each minute), ``denom_x = b·Σr² − (Σr)²`` is a difference of near-equal sums whose true relative
variance ``denom_x/(b·Σr²) = CoV²`` is ~1e-12 — right AT the old ``denom_x > 1e-12·(Σx)²`` guard floor. There
the batch fresh-sum ``Σr²`` and the incremental running-sum ``Σr²`` round it onto OPPOSITE sides of the floor →
a null/non-null parity FLIP (measured worst self-check tol-ratio up to +Inf), and even when both keep it the
surviving corr is the correlation of float noise.

A return has NO stable per-symbol anchor (unlike volume/close) and NO rolling origin (unlike the time axis), so
the y-side / time-axis conditioning does not apply. The fix is the translation-invariant relative-variance guard
``_OLS_DENOM_X_CENTERED_REL_EPS`` (1e-9): reject ``denom_x ≤ 1e-9·(b·Σr²)`` so a constant-return window is NULL
on BOTH paths. It ships UNCONDITIONALLY in the shared OLS stat (all three twins), in ADDITION to the raw (Σx)²
floor (can only null more cells, never resurrect one), and is value-identical on well-conditioned cells. With
this x-guard AND the FP_RUST_REDUCE y-anchor both active, ``price_volume.incremental_safe`` is True.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features import declarative
from quantlib.features.base import BatchContext
from quantlib.features.declarative import ReductionGroup, compute_reduction_batch
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.reduction_anchor import attach_reduction_anchors
from quantlib.features.registry import REGISTRY

BASE = dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.timezone.utc)
GROUP_NAME = "price_volume"
DEEPEST_WINDOW_M = 180
_BREACH_RATIO = 10.0  # the production self-check breach threshold (capture._PARITY_BREACH_RATIO)


class _rust_reduce:
    """Toggle FP_RUST_REDUCE (the y-anchor gate) for the duration of a test — the x-guard is unconditional, but
    the x-side breach must be probed with the y-side already conditioned (the real arming config)."""

    def __init__(self, on: bool) -> None:
        self.on = on
        self.prev = declarative._USE_RUST_REDUCE

    def __enter__(self) -> "_rust_reduce":
        declarative._USE_RUST_REDUCE = self.on
        return self

    def __exit__(self, *exc: object) -> None:
        declarative._USE_RUST_REDUCE = self.prev


class _xguard_off:
    """Disable ONLY the x-side centered-variance guard (eps → 0) so the raw (Σx)² floor alone decides — the
    pre-fix behavior, used to prove the fixture genuinely breaches without the guard (non-vacuous)."""

    def __init__(self) -> None:
        self.prev = declarative._OLS_DENOM_X_CENTERED_REL_EPS

    def __enter__(self) -> "_xguard_off":
        declarative._OLS_DENOM_X_CENTERED_REL_EPS = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        declarative._OLS_DENOM_X_CENTERED_REL_EPS = self.prev


def _group() -> ReductionGroup:
    groups = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name == GROUP_NAME]
    assert groups, f"{GROUP_NAME} missing from registry"
    return groups[0]


def _anchored(frame: pl.DataFrame) -> pl.DataFrame:
    daily = (
        frame.group_by("symbol")
        .agg(pl.col("volume").sum().alias("volume"), pl.col("close").last().alias("close"))
        .with_columns(pl.lit(1).alias("date"))
    )
    return attach_reduction_anchors({"minute_agg": frame, "daily": daily})["minute_agg"]


def _constant_return_frame(n_sym: int, n_min: int, seed: int) -> pl.DataFrame:
    """A near-CONSTANT per-minute return stream (drift 1e-3 with ~1e-9 jitter → return constant to ~6 sig figs,
    CoV² ~1e-12) — the degenerate x-side corner that flips denom_x null/non-null. Volume varies freely (the
    y-side is well-conditioned), so this isolates the RETURN x-side cancellation. Sparse presence on half the
    names so the incremental sparse-fold path is exercised."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 50.0 + 30.0 * s for s in range(n_sym)}
    base_vol = {s: 1.0e6 * (s + 1) for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            if not (mi == 0 or rng.random() < (0.4 if s % 2 == 0 else 0.95)):
                continue
            ret = (
                1.0e-3 + rng.standard_normal() * 1.0e-9
            )  # near-constant: Σr²−(Σr)²/n catastrophically cancels
            price[s] *= 1.0 + ret
            close = price[s]
            vol = base_vol[s] * (1.0 + rng.standard_normal() * 0.3)  # volume varies (corr defined on y)
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "high": close * 1.001,
                    "low": close * 0.999,
                    "close": close,
                    "volume": vol,
                }
            )
    frame = (
        pl.DataFrame(rows).with_columns(pl.col("minute").dt.cast_time_unit("us")).sort(["symbol", "minute"])
    )
    return _anchored(frame)


def _realistic_frame(n_sym: int, n_min: int, seed: int) -> pl.DataFrame:
    """Real-ish intraday: returns OSCILLATE (std ~1e-3..5e-3, CoV² ≫ 1), volume varies — the well-conditioned
    reference where the x-guard must change NOTHING."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 30.0 + 15.0 * s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            ret = rng.standard_normal() * (0.001 + 0.004 * (s % 3))
            price[s] *= 1.0 + ret
            close = price[s]
            vol = 1.0e5 * (s + 1) * (1.0 + abs(rng.standard_normal()))
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "high": close * 1.003,
                    "low": close * 0.997,
                    "close": close,
                    "volume": vol,
                }
            )
    frame = (
        pl.DataFrame(rows).with_columns(pl.col("minute").dt.cast_time_unit("us")).sort(["symbol", "minute"])
    )
    return _anchored(frame)


def _worst_tol_ratio(batch: pl.DataFrame, inc: pl.DataFrame, prefix: str) -> float:
    """The production self-check ratio over the named feature columns: max |a−b| / (atol + rtol·|a|);
    inf on a null/non-null flip. Mirrors capture._incremental_parity."""
    atol, rtol = 1e-9, 1e-6
    cols = [c for c in batch.columns if c.startswith(prefix) and c not in ("symbol", "minute")]
    joined = batch.select(["symbol", *cols]).join(
        inc.select(["symbol", *cols]), on="symbol", how="inner", suffix="__i"
    )
    worst = 0.0
    for col in cols:
        a, b = pl.col(col), pl.col(f"{col}__i")
        if joined.filter(a.is_null() != b.is_null()).height:
            return float("inf")
        ratio = joined.select(((a - b).abs() / (atol + rtol * a.abs())).fill_null(0.0).max()).item()
        if ratio is not None:
            worst = max(worst, float(ratio))
    return worst


def _sweep_worst(group: ReductionGroup, stream: pl.DataFrame) -> tuple[float, int]:
    """Step the incremental engine minute-by-minute, comparing pv_correlation to batch compute_latest after the
    deepest window warms. Returns (worst tol-ratio, minutes graded)."""
    minutes = sorted(stream["minute"].unique())
    engine = IncrementalEngine([group])
    worst, graded = 0.0, 0
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        inc = engine.step(buffer, slice_derive=True)[GROUP_NAME]
        if ti <= DEEPEST_WINDOW_M:
            continue
        graded += 1
        batch = group.compute_latest(BatchContext(frames={"minute_agg": buffer}))
        worst = max(worst, _worst_tol_ratio(batch, inc, "pv_correlation"))
    return worst, graded


def test_xside_off_breaches_constant_return() -> None:
    """NON-VACUOUS: with the x-side centered-variance guard DISABLED (raw (Σx)² floor only — the pre-fix
    behavior) the incremental running-Σr² diverges from the batch fresh-sum on a near-constant-return window,
    breaching the production self-check (the return-side cancellation that kept the group parked even after the
    y-side was conditioned). FP_RUST_REDUCE on so the y-side is already conditioned — this isolates the x-side.
    """
    group = _group()
    stream = _constant_return_frame(n_sym=12, n_min=210, seed=3)
    with _rust_reduce(True), _xguard_off():
        worst, graded = _sweep_worst(group, stream)
    assert graded > 0, "no post-180m-warmup minutes graded — stream must exceed the deepest window"
    assert worst > _BREACH_RATIO, f"fixture did not exercise the x-side breach (worst {worst:.3g})"


def test_xside_guard_clears_the_breach() -> None:
    """⭐ The fix: with the x-side centered-variance guard ON (default) a near-constant-return window is NULL on
    BOTH the incremental and batch paths, so pv_correlation's self-check tol-ratio collapses under the breach
    threshold on the SAME constant-return fixture that breaches when the guard is disabled."""
    group = _group()
    stream = _constant_return_frame(n_sym=12, n_min=210, seed=3)
    with _rust_reduce(True):
        worst, graded = _sweep_worst(group, stream)
    assert graded > 0
    assert worst < _BREACH_RATIO, f"x-guard did not clear the breach: worst tol-ratio {worst:.3g}"


def test_xside_guard_value_identical_on_well_conditioned() -> None:
    """The x-guard nulls ONLY degenerate cells: on a realistic oscillating-return stream every batch
    pv_correlation cell is byte-identical with the guard ON vs disabled (no legitimate corr lost, no value
    changed) — the fp-unchanged / trust-preserved guarantee."""
    group = [_group()]
    frame = _realistic_frame(n_sym=10, n_min=200, seed=21)
    ctx = BatchContext(frames={"minute_agg": frame})
    with _rust_reduce(True):
        on = compute_reduction_batch(group, ctx)[GROUP_NAME].sort("symbol")
        with _xguard_off():
            off = compute_reduction_batch(group, ctx)[GROUP_NAME].sort("symbol")
    for col in [c for c in on.columns if c.startswith("pv_correlation")]:
        joined = on.select("symbol", col).join(off.select("symbol", pl.col(col).alias("_off")), on="symbol")
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_off").is_null())
                | ((pl.col(col) - pl.col("_off")).abs() <= 1e-12)
            )
        )
        assert bad.height == 0, f"{col}: x-guard changed a well-conditioned value\n{bad.head()}"
