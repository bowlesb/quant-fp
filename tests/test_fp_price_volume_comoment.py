"""FP_RUST_REDUCE y-centers price_volume.pv_correlation's RAW-share-volume regressand — reusing the EXISTING
close-y anchoring mechanism from test_fp_rust_reduce (no new engine/kernel path). pv_correlation is a Pearson
corr of one-minute RETURN (x, ~1e-4) against raw share VOLUME (y, ~1e6), so the large-magnitude cancellation is
on the Y side: ``denom_y = b·Σy² − (Σy)²`` is a difference of near-equal large sums whose low bits the
incremental running-Σy² rounds differently from the batch fresh-sum — measured worst self-check tol-ratio ~670×
the production breach threshold, which is why the group was parked (``incremental_safe=False``). Centering ``y``
on the per-symbol-constant ``__anchor_volume`` (the same per-minute-scale anchor the ``volume`` group's centered
std already reads) conditions ``denom_y`` on small centered volume, so both paths round it IDENTICALLY.

OLS corr is translation-invariant in y, so the value is identical to the raw form in exact arithmetic — only
the float conditioning changes (fp unchanged, verified by ``test_y_centering_value_identical_off_vs_on``). The
flag is default OFF; with it off the group stays parked (raw cancellation, incremental_safe=False) — these
tests drive both states. OBV-slope (the other regression) is on a centered time axis and is NOT y-centered.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

from quantlib.features import declarative
from quantlib.features.base import BatchContext
from quantlib.features.declarative import (
    ReductionGroup,
    _anchored_namespaces,
    compute_reduction_batch,
)
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.reduction_anchor import anchor_column, attach_reduction_anchors
from quantlib.features.registry import REGISTRY

BASE = dt.datetime(2026, 3, 2, 14, 30, tzinfo=dt.timezone.utc)
GROUP_NAME = "price_volume"
DEEPEST_WINDOW_M = 180
_BREACH_RATIO = 10.0  # the production self-check breach threshold (capture._PARITY_BREACH_RATIO)


class _flag:
    """Toggle the module-level FP_RUST_REDUCE gate (read once at import) for the duration of a test."""

    def __init__(self, on: bool) -> None:
        self.on = on
        self.prev = declarative._USE_RUST_REDUCE

    def __enter__(self) -> "_flag":
        declarative._USE_RUST_REDUCE = self.on
        return self

    def __exit__(self, *exc: object) -> None:
        declarative._USE_RUST_REDUCE = self.prev


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


def _large_volume_frame(n_sym: int, n_min: int, present_p: float, seed: int) -> pl.DataFrame:
    """A LARGE-magnitude raw-share-volume stream (~1e6) with sparse/gappy presence and near-flat per-minute
    moves — the regime where the corr y-side ``denom_y`` on raw volume catastrophically cancels (the breach this
    fix closes). The volume anchor is attached as production does, so the group is y-centered under the flag."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 50.0 + 30.0 * s for s in range(n_sym)}
    base_vol = {s: 1.0e6 * (s + 1) for s in range(n_sym)}  # large-magnitude regressand: denom_y cancellation
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            if not (mi == 0 or rng.random() < (present_p if s % 2 == 0 else 0.95)):
                continue
            price[s] *= 1.0 + rng.standard_normal() * 1e-4  # near-flat moves
            close = price[s]
            vol = base_vol[s] * (1.0 + rng.standard_normal() * 1e-5)  # near-constant large volume
            rows.append(
                {"symbol": f"S{s}", "minute": minute, "high": close * 1.001, "low": close * 0.999,
                 "close": close, "volume": vol}
            )
    frame = (
        pl.DataFrame(rows).with_columns(pl.col("minute").dt.cast_time_unit("us")).sort(["symbol", "minute"])
    )
    return _anchored(frame)


def _well_conditioned_frame(n_sym: int, n_min: int, seed: int) -> pl.DataFrame:
    """A moderate-vol drift stream with moderate volume — well-conditioned corr, the reference for value
    equality of y-centering ON vs OFF (the fp-unchanged guarantee)."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 45.0 + 20.0 * s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + rng.standard_normal() * 0.02
            close = price[s]
            vol = 1.0e5 * (s + 1) * (1.0 + rng.random())
            rows.append(
                {"symbol": f"S{s}", "minute": minute, "high": close * 1.002, "low": close * 0.998,
                 "close": close, "volume": vol}
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


def test_declares_volume_y_anchor_under_flag() -> None:
    """pv_correlation declares exactly its volume-y anchor (and only ``pv``, not the time-axis ``obv``);
    ``_anchored_namespaces`` keys it. Flag OFF → no anchored namespace (parked, raw form)."""
    group = _group()
    assert group.regression_y_anchor() == {"pv": anchor_column("volume")}
    with _flag(True):
        anchored = _anchored_namespaces([group])
        assert anchored == {"0_pv"}  # only pv is y-centered; the OBV-slope time regression is not
    with _flag(False):
        assert _anchored_namespaces([group]) == set()


def test_stays_parked_until_lead_arms() -> None:
    """The conditioning MECHANISM ships here, but ``incremental_safe`` stays False (parked on the batch fresh-sum
    recompute, byte-identical under FP_INCREMENTAL) regardless of the flag — same posture as the y-anchored
    groups. The flip to True is the Lead's enablement call after a real-data soak (this test guards against an
    accidental premature un-gate; the parity readiness is proven separately by the breach-clears test below)."""
    group = _group()
    with _flag(False):
        assert group.incremental_safe is False
    with _flag(True):
        assert group.incremental_safe is False


def test_y_centering_value_identical_off_vs_on() -> None:
    """On a well-conditioned stream the y-centered batch (flag ON) produces the SAME pv_correlation features as
    OFF to machine precision — the fingerprint-unchanged / trust-preserved guarantee (OLS is translation-
    invariant in y). Null masks identical."""
    group = [_group()]
    frame = _well_conditioned_frame(n_sym=8, n_min=120, seed=11)
    ctx = BatchContext(frames={"minute_agg": frame})
    with _flag(False):
        off = compute_reduction_batch(group, ctx)[GROUP_NAME].sort("symbol")
    with _flag(True):
        on = compute_reduction_batch(group, ctx)[GROUP_NAME].sort("symbol")
    assert off.columns == on.columns
    for col in [c for c in off.columns if c not in ("symbol", "minute")]:
        joined = off.select("symbol", col).join(
            on.select("symbol", pl.col(col).alias("_on")), on="symbol"
        )
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_on").is_null())
                | ((pl.col(col) - pl.col("_on")).abs() <= 1e-9 + 1e-9 * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"{col}: y-centering changed a value\n{bad.head()}"


def test_y_centering_off_still_breaches_large_volume() -> None:
    """With the flag OFF (parked, raw form) the incremental running-Σx² diverges from the batch fresh-sum on a
    large-magnitude volume window — the breach that justifies keeping the group parked when uncentered. This is
    the non-vacuous companion to the ON test: it proves the fixture genuinely exercises the breach class."""
    group = _group()
    stream = _large_volume_frame(n_sym=12, n_min=210, present_p=0.4, seed=3)
    minutes = sorted(stream["minute"].unique())
    with _flag(False):
        engine = IncrementalEngine([group])
        worst = 0.0
        for ti, minute in enumerate(minutes):
            buffer = stream.filter(pl.col("minute") <= minute)
            inc = engine.step(buffer, slice_derive=True)[GROUP_NAME]
            if ti <= DEEPEST_WINDOW_M:
                continue
            batch = group.compute_latest(BatchContext(frames={"minute_agg": buffer}))
            worst = max(worst, _worst_tol_ratio(batch, inc, "pv_correlation"))
    assert worst > _BREACH_RATIO, f"fixture did not exercise the breach (worst {worst:.3g}) — make it harsher"


def test_y_centering_on_clears_the_breach() -> None:
    """⭐ The fix: with the flag ON the y-centered ``denom_y`` rounds IDENTICALLY on the incremental running-sum
    and the batch fresh-sum paths, so pv_correlation's self-check tol-ratio collapses well under the production
    breach threshold on the SAME large-volume fixture that breaches at ~670× when uncentered."""
    group = _group()
    stream = _large_volume_frame(n_sym=12, n_min=210, present_p=0.4, seed=3)
    minutes = sorted(stream["minute"].unique())
    with _flag(True):
        engine = IncrementalEngine([group])
        worst = 0.0
        graded = 0
        for ti, minute in enumerate(minutes):
            buffer = stream.filter(pl.col("minute") <= minute)
            inc = engine.step(buffer, slice_derive=True)[GROUP_NAME]
            if ti <= DEEPEST_WINDOW_M:
                continue
            graded += 1
            batch = group.compute_latest(BatchContext(frames={"minute_agg": buffer}))
            worst = max(worst, _worst_tol_ratio(batch, inc, "pv_correlation"))
    assert graded > 0, "no post-180m-warmup minutes graded — stream must exceed the deepest window"
    assert worst < _BREACH_RATIO, f"y-centering did not clear the breach: worst tol-ratio {worst:.3g}"


@pytest.mark.parametrize("seed", [1, 7, 13])
def test_incremental_matches_batch_all_pv_features(seed: int) -> None:
    """Flag ON: the full incremental step() equals batch compute_latest cell-for-cell for EVERY price_volume
    feature (not just pv_correlation) on a large-volume sparse stream — the ratios/vwap/obv_slope ride the same
    fold and must stay parity-true after the volume y-anchor wiring."""
    group = _group()
    stream = _large_volume_frame(n_sym=10, n_min=205, present_p=0.5, seed=seed)
    minutes = sorted(stream["minute"].unique())
    with _flag(True):
        engine = IncrementalEngine([group])
        for ti, minute in enumerate(minutes):
            buffer = stream.filter(pl.col("minute") <= minute)
            inc = engine.step(buffer, slice_derive=True)[GROUP_NAME]
            if ti <= DEEPEST_WINDOW_M:
                continue
            batch = group.compute_latest(BatchContext(frames={"minute_agg": buffer}))
            worst = _worst_tol_ratio(batch, inc, "")  # all feature columns
            assert worst < _BREACH_RATIO, f"min {minute}: incremental != batch (worst {worst:.3g})"
