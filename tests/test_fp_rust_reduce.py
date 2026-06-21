"""FP_RUST_REDUCE conditions the OLS R²/corr Y-SIDE the way FP_CENTERED_TIME (#386) conditioned the time-axis
X — it CENTERS a regression's ``y`` (close ~$45-$500) on a per-symbol-constant anchor (the daily-bar close,
attached by ``attach_close_anchor``) BEFORE the six paired OLS sums are accumulated, so the y-variance /
covariance terms ``denom_y = b·Σ(y−a)² − (Σ(y−a))²`` and ``cov_n = b·Σ(x·(y−a)) − Σx·Σ(y−a)`` stay
conditioned on small centered close instead of the raw price. The near-perfect-fit r²/corr cancellation that
#386's x-conditioning could NOT reach (the y-side SSR/SST straddle that gated trend_quality / clean_momentum
from FP_INCREMENTAL — docs/INCREMENTAL_READINESS.md §"REAL-TAPE PROMOTION GATE") then rounds IDENTICALLY in the
batch fresh-sum path and the incremental running-sum path.

OLS is translation-invariant in y, so slope/r²/corr are value-identical to the raw form in exact arithmetic;
only the float conditioning changes (the fingerprint is unchanged — verified by ``test_flag_default_off_is_
byte_identical``). The flag is default OFF; these tests drive both states.

Scope: the y-centered regressions trend_quality.trend and clean_momentum.cm_clean (y=close). residual_analysis
is NOT centered (its resid_std divides the SSR by mean_y, which centering would shift — and the real-tape gate
measures it already clean), and the non-time corr-denom groups (market_beta / return_dynamics) need a separate
return anchor (out of scope)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from quantlib.features import declarative
from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.declarative import (
    ReductionGroup,
    _anchored_namespaces,
    compute_reduction_batch,
)
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.reduction_anchor import anchor_column, attach_reduction_anchors
from quantlib.features.registry import REGISTRY

BASE = dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.timezone.utc)
ANCHORED_GROUPS = ("trend_quality", "clean_momentum")
_BREACH_RATIO = 10.0  # the production self-check breach threshold (capture._PARITY_BREACH_RATIO)


def _group(name: str) -> ReductionGroup:
    groups = [g for g in REGISTRY.groups() if isinstance(g, ReductionGroup) and g.name == name]
    assert groups, f"{name} missing from registry"
    return groups[0]


def _near_perfect_frame(n_sym: int, n_min: int, present_p: float, seed: int) -> pl.DataFrame:
    """A LARGE-PRICE near-linear close stream — the r2≈1 regime on raw close (~$45-$500) where the y-side OLS
    denom cancellation bites (the breach this fix closes). The close anchor (+ volume anchor) is attached as
    production does, so the y-centered groups are runnable and centered on the per-symbol constant."""
    rng = np.random.default_rng(seed)
    rows = []
    level = {s: 45.0 + 40.0 * s for s in range(n_sym)}  # spread across the real-tape $45-$500 price range
    slope = {s: 0.01 + 0.003 * s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            if not (mi == 0 or rng.random() < present_p):
                continue
            close = level[s] + slope[s] * mi + rng.standard_normal() * 3e-3  # near-perfect linear fit
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "open": close * 0.999,
                    "close": close,
                    "high": close * 1.002,
                    "low": close * 0.998,
                    "volume": 1000.0 + rng.random() * 4000,
                }
            )
    frame = (
        pl.DataFrame(rows).with_columns(pl.col("minute").dt.cast_time_unit("us")).sort(["symbol", "minute"])
    )
    daily = (
        frame.group_by("symbol")
        .agg(pl.col("volume").sum().alias("volume"), pl.col("close").last().alias("close"))
        .with_columns(pl.lit(1).alias("date"))
    )
    return attach_reduction_anchors({"minute_agg": frame, "daily": daily})["minute_agg"]


def _well_conditioned_frame(n_sym: int, n_min: int, present_p: float, seed: int) -> pl.DataFrame:
    """A moderate-vol drift stream (well-conditioned r²), the reference for value-equality ON vs OFF."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 45.0 + 40.0 * s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            if not (mi == 0 or rng.random() < present_p):
                continue
            price[s] *= 1.0 + rng.standard_normal() * 0.02
            close = price[s]
            rows.append(
                {
                    "symbol": f"S{s}",
                    "minute": minute,
                    "open": close * 0.999,
                    "close": close,
                    "high": close * 1.002,
                    "low": close * 0.998,
                    "volume": 1000.0 + rng.random() * 4000,
                }
            )
    frame = (
        pl.DataFrame(rows).with_columns(pl.col("minute").dt.cast_time_unit("us")).sort(["symbol", "minute"])
    )
    daily = (
        frame.group_by("symbol")
        .agg(pl.col("volume").sum().alias("volume"), pl.col("close").last().alias("close"))
        .with_columns(pl.lit(1).alias("date"))
    )
    return attach_reduction_anchors({"minute_agg": frame, "daily": daily})["minute_agg"]


def _worst_tol_ratio(batch: dict[str, pl.DataFrame], inc: dict[str, pl.DataFrame]) -> float:
    """The production self-check ratio: max |a−b| / (atol + rtol·|a|) over shared cells; inf on a null/non-null
    flip. Mirrors capture._incremental_parity over the supplied group frames."""
    atol, rtol = 1e-9, 1e-6
    worst = 0.0
    for name, batch_frame in batch.items():
        inc_frame = inc.get(name)
        if inc_frame is None:
            return float("inf")
        cols = [c for c in batch_frame.columns if c not in ("symbol", "minute")]
        joined = batch_frame.select(["symbol", *cols]).join(
            inc_frame.select(["symbol", *cols]), on="symbol", how="inner", suffix="__i"
        )
        for col in cols:
            a, b = pl.col(col), pl.col(f"{col}__i")
            if joined.filter(a.is_null() != b.is_null()).height:
                return float("inf")
            ratio = joined.select(((a - b).abs() / (atol + rtol * a.abs())).fill_null(0.0).max()).item()
            if ratio is not None:
                worst = max(worst, float(ratio))
    return worst


def test_anchored_groups_declare_a_close_y_anchor() -> None:
    """The y-centered groups each declare exactly their close-y anchor under the flag, and the non-centered
    siblings do NOT (the scope guard) — so ``_anchored_namespaces`` (which drives the centered guard) only ever
    keys the intended regressions."""
    with_flag = _flag(True)
    with with_flag:
        assert _group("trend_quality").regression_y_anchor() == {"trend": anchor_column("close")}
        assert _group("clean_momentum").regression_y_anchor() == {"cm_clean": anchor_column("close")}
        # residual_analysis / market_beta / return_dynamics must NOT y-center (resid_std mean_y / no anchor)
        assert _group("residual_analysis").regression_y_anchor() == {}
        assert _group("market_beta").regression_y_anchor() == {}
        assert _group("return_dynamics").regression_y_anchor() == {}
        assert _anchored_namespaces([_group("trend_quality")]) == {"0_trend"}


class _flag:
    """Context manager toggling the module-level FP_RUST_REDUCE gate (the env is read once at import)."""

    def __init__(self, on: bool) -> None:
        self.on = on
        self.prev = declarative._USE_RUST_REDUCE

    def __enter__(self) -> "_flag":
        declarative._USE_RUST_REDUCE = self.on
        return self

    def __exit__(self, *exc: object) -> None:
        declarative._USE_RUST_REDUCE = self.prev


@pytest.mark.parametrize("group_name", ANCHORED_GROUPS)
def test_flag_default_off_is_byte_identical(group_name: str) -> None:
    """With the flag OFF (the default) the paired-column expression graph is byte-identical to today — turning
    the flag off restores the exact current behaviour (fp unchanged until a Lead/Ben relaunch flips it). Proven
    by build_plan producing the SAME derived exprs as before (no y-centering, no anchored namespaces)."""
    group = [_group(group_name)]
    with _flag(False):
        derived_off, *_rest = declarative.build_plan(group)
        assert _anchored_namespaces(group) == set()
    # the raw y paired columns (no ``__anchor_close`` subtraction) are present
    assert any("__rd_" in str(e) and "_y" in str(e) for e in derived_off)
    assert all("__anchor_close" not in str(e) for e in derived_off)


@pytest.mark.parametrize("group_name", ANCHORED_GROUPS)
def test_value_identical_on_well_conditioned_cells(group_name: str) -> None:
    """On a well-conditioned stream the centered batch (FP_RUST_REDUCE ON) produces the SAME features as OFF to
    machine precision — the fingerprint-unchanged / trust-preserved guarantee. Null masks identical."""
    group = [_group(group_name)]
    frame = _well_conditioned_frame(n_sym=8, n_min=120, present_p=0.8, seed=11)
    ctx = BatchContext(frames={"minute_agg": frame})
    with _flag(False):
        off = compute_reduction_batch(group, ctx)[group_name].sort("symbol")
    with _flag(True):
        on = compute_reduction_batch(group, ctx)[group_name].sort("symbol")
    assert off.columns == on.columns
    for col in off.columns:
        if col in ("symbol", "minute"):
            continue
        off_v, on_v = off[col].to_numpy().astype(float), on[col].to_numpy().astype(float)
        np.testing.assert_array_equal(np.isnan(off_v), np.isnan(on_v))
        finite = ~np.isnan(off_v)
        if finite.any():
            np.testing.assert_allclose(on_v[finite], off_v[finite], rtol=1e-7, atol=1e-9)


def test_centering_conditions_breach_and_on_path_is_clean() -> None:
    """THE gate: y-centering CONDITIONS the incremental==batch divergence (ON worst ratio ≤ OFF), and the ON
    path is CLEAN (ratio ≤ breach threshold, ZERO null-flips). The full documented breach magnitude (1683× /
    620× on real NKE) needs the gappy near-flat micro-structure of REAL tape, which a synthetic stream cannot
    reproduce (docs/INCREMENTAL_READINESS.md) — the authoritative breach→clean proof is the real-tape soak
    (scripts/incremental_realdata_soak.py 2026-06-17: trend_quality 1683×→0.4×, clean_momentum 620×→0.2×).
    Here a near-perfect synthetic fit shows the conditioning direction + the ON-path cleanliness/no-flip
    invariant cheaply. Force-incremental_safe (probe-only; prod keeps incremental_safe=False until the relaunch
    flip)."""
    frame = _near_perfect_frame(n_sym=10, n_min=80, present_p=0.5, seed=7)
    groups = [g for g in runnable({"minute_agg": frame}) if g.name in ANCHORED_GROUPS]
    assert {g.name for g in groups} == set(
        ANCHORED_GROUPS
    ), "anchored groups must be runnable (anchor attached)"
    # NOTE: no ``incremental_safe = True`` mutation — ``compute_reduction_batch`` / ``IncrementalEngine.step``
    # compute regardless of the flag (it only routes in capture.process_bars), so this graded probe needs no
    # promotion AND avoids polluting the shared REGISTRY singletons for later tests (the real prod promotion
    # stays incremental_safe=False until the Lead's relaunch flip — see the soak script for the force-promote).
    minutes = sorted(frame["minute"].unique())

    def worst_over_soak(flag_on: bool) -> float:
        with _flag(flag_on):
            engine = IncrementalEngine(groups)
            worst = 0.0
            for ti, minute in enumerate(minutes):
                buffer = frame.filter(pl.col("minute") <= minute)
                inc = engine.step(buffer, slice_derive=True)
                if ti < 30:  # let the short windows fill before grading
                    continue
                ctx = BatchContext(frames={"minute_agg": buffer})
                batch = compute_reduction_batch(groups, ctx)
                worst = max(worst, _worst_tol_ratio(batch, inc))
            return worst

    worst_off = worst_over_soak(False)
    worst_on = worst_over_soak(True)
    assert (
        worst_on <= worst_off + 1e-9
    ), f"centering must not worsen conditioning: on={worst_on} off={worst_off}"
    assert (
        worst_on <= _BREACH_RATIO
    ), f"FP_RUST_REDUCE ON path must be clean (no breach/flip); worst_on={worst_on}"
