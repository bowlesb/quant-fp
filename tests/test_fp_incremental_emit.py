"""Numpy-emit parity gate: ``IncrementalEngine.step_numpy()`` (canonical columns built directly from the
running-sum numpy array, bypassing the polars pivot in ``assemble_from_long``) == the polars ``step()`` AND
the batch ``compute_latest()``, cell-for-cell within each feature's tolerance, across a minute stream.

The representative group is price_volume — it exercises EVERY accessor the numpy emit must reproduce: sum +
mean reductions, an OLS correlation (``pv``), and an OLS slope on a CUMULATIVE+time stateful regression
(``obv``, the hardest path). std/r2/mean_y are simpler variants of the same algebra (mean-machinery and the
corr denominators respectively); a synthetic group below pins those two extra accessors directly.

If this stays green, the numpy emit is parity-true by construction for the price_volume accessor surface.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext, FeatureSpec, FeatureType, InputSpec
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup, mean_y_, r2_, std_
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.reduction_anchor import attach_reduction_anchors

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)
GROUP_NAME = "price_volume"


def _stream(n_sym: int = 8, n_min: int = 70) -> pl.DataFrame:
    rng = np.random.default_rng(7)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + (rng.standard_normal() * 0.002)
            c = price[s]
            rows.append(
                {"symbol": f"S{s}", "minute": minute, "open": c * 0.999, "high": c * 1.002, "low": c * 0.998,
                 "close": c, "volume": 1000.0 + rng.random() * 4000, "n_trades": float(rng.integers(1, 200)),
                 "signed_volume": rng.standard_normal() * 1000, "mean_spread_bps": rng.random() * 5,
                 "quote_imbalance": rng.standard_normal() * 0.3, "mean_bid_size": rng.random() * 100,
                 "mean_ask_size": rng.random() * 100}
            )
    frame = pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
    # Attach the per-symbol volume/close centering anchors production attaches before either path runs, so
    # price_volume (whose InputSpec declares __anchor_volume for its x-side pv_correlation conditioning) is
    # runnable. Value-additive — the anchors are only consumed under FP_RUST_REDUCE, never change a value.
    daily = (
        frame.group_by("symbol")
        .agg(pl.col("volume").sum().alias("volume"), pl.col("close").last().alias("close"))
        .with_columns(pl.lit(1).alias("date"))
    )
    return attach_reduction_anchors({"minute_agg": frame, "daily": daily})["minute_agg"]


def _assert_cellwise(reference: pl.DataFrame, candidate: pl.DataFrame, label: str, tol: float = 1e-6) -> None:
    assert set(candidate.columns) == set(reference.columns), f"{label}: columns differ"
    reference = reference.sort("symbol")
    candidate = candidate.sort("symbol").select(reference.columns)
    for col in [c for c in reference.columns if c not in ("symbol", "minute")]:
        joined = reference.select("symbol", col).join(
            candidate.select("symbol", pl.col(col).alias("_i")), on="symbol"
        )
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_i").is_null())
                | ((pl.col(col) - pl.col("_i")).abs() <= tol + tol * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"{label}.{col}: {bad.height} mismatches\n{bad.head()}"


def test_step_numpy_matches_polars_step_price_volume() -> None:
    """The numpy emit path equals the polars assemble path for price_volume, every minute (incl. warmup nulls).
    Two engines fed the identical stream — only the assemble differs — so any divergence is the numpy emit's."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    polars_engine = IncrementalEngine(groups)
    numpy_engine = IncrementalEngine(groups)

    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        polars_out = polars_engine.step(buffer)[GROUP_NAME]
        numpy_out = numpy_engine.step_numpy(buffer)[GROUP_NAME]
        _assert_cellwise(polars_out, numpy_out, f"min{minute}", tol=0.0)  # IDENTICAL: same sums, same algebra


def test_step_numpy_matches_batch_price_volume() -> None:
    """The numpy emit path equals the batch ``compute_latest`` (the live source of truth) for price_volume,
    at warmup/mid/full-buffer checkpoints — closing backfill==batch==incremental(numpy-emit) for this group."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    pv_group = next(g for g in groups if g.name == GROUP_NAME)
    engine = IncrementalEngine(groups)

    checkpoints = {10, 30, len(minutes) - 1}
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        numpy_out = engine.step_numpy(buffer)[GROUP_NAME]
        if ti in checkpoints:
            ctx = BatchContext(frames={"minute_agg": buffer})
            tol = max(spec.tolerance for spec in pv_group.declare())
            _assert_cellwise(pv_group.compute_latest(ctx), numpy_out, f"batch-min{ti}", tol=max(tol, 1e-6))


class _StdR2Group(ReductionGroup):
    """A synthetic reduction group that pins the std and r2/mean_y accessors of the numpy emit (price_volume
    has neither). std over a value reduced with ('std',); a self-regression giving r2 and mean_y."""

    name = "_std_r2_probe"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume")),)
    _windows = (5, 10, 20)

    def declare(self):  # type: ignore[no-untyped-def]
        specs = []
        for w in self._windows:
            specs.append(FeatureSpec(name=f"probe_std_{w}m", description="std probe", dtype="Float64",
                                     nan_policy="warmup", layer="A", tolerance=1e-4))
            specs.append(FeatureSpec(name=f"probe_r2_{w}m", description="r2 probe", dtype="Float64",
                                     nan_policy="warmup", layer="A", tolerance=1e-4))
            specs.append(FeatureSpec(name=f"probe_meany_{w}m", description="mean_y probe", dtype="Float64",
                                     nan_policy="warmup", layer="A", tolerance=1e-4))
        return specs

    def reduced(self):  # type: ignore[no-untyped-def]
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        return {"ret": (ret, ("std",), self._windows)}

    def regressions(self):  # type: ignore[no-untyped-def]
        return {"cv": (pl.col("volume"), pl.col("close"), ("r2", "mean_y"), self._windows)}

    def assemble(self):  # type: ignore[no-untyped-def]
        feats = {}
        for w in self._windows:
            feats[f"probe_std_{w}m"] = std_("ret", w)
            feats[f"probe_r2_{w}m"] = r2_("cv", w)
            feats[f"probe_meany_{w}m"] = mean_y_("cv", w)
        return feats


def test_step_numpy_std_r2_meany_accessors() -> None:
    """std, r2, mean_y numpy-emit accessors (absent from price_volume) match the polars assemble path."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    group = _StdR2Group()
    polars_engine = IncrementalEngine([group])
    numpy_engine = IncrementalEngine([group])
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        polars_out = polars_engine.step(buffer)[group.name]
        numpy_out = numpy_engine.step_numpy(buffer)[group.name]
        _assert_cellwise(polars_out, numpy_out, f"probe-min{minute}", tol=0.0)


def test_step_rust_matches_numpy_step_price_volume() -> None:
    """The Rust-assemble path (``step_rust``) equals the numpy-emit AND polars-assemble paths for price_volume,
    every minute (incl. warmup nulls). The Rust ``assemble_canonical`` kernel produces the canonical
    sum/mean/OLS-corr/OLS-slope columns; identical sums + identical algebra => IDENTICAL (tol 0)."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    polars_engine = IncrementalEngine(groups)
    numpy_engine = IncrementalEngine(groups)
    rust_engine = IncrementalEngine(groups)
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        polars_out = polars_engine.step(buffer)[GROUP_NAME]
        numpy_out = numpy_engine.step_numpy(buffer)[GROUP_NAME]
        rust_out = rust_engine.step_rust(buffer)[GROUP_NAME]
        _assert_cellwise(numpy_out, rust_out, f"rust-vs-numpy-min{minute}", tol=0.0)
        _assert_cellwise(polars_out, rust_out, f"rust-vs-polars-min{minute}", tol=0.0)


def test_step_rust_matches_batch_price_volume() -> None:
    """The Rust-assemble path equals the batch ``compute_latest`` (live source of truth) for price_volume —
    closing backfill==batch==incremental(rust-emit), proving corr/slope/mean/sum cell-for-cell."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    pv_group = next(g for g in groups if g.name == GROUP_NAME)
    engine = IncrementalEngine(groups)
    checkpoints = {10, 30, len(minutes) - 1}
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        rust_out = engine.step_rust(buffer)[GROUP_NAME]
        if ti in checkpoints:
            ctx = BatchContext(frames={"minute_agg": buffer})
            tol = max(spec.tolerance for spec in pv_group.declare())
            _assert_cellwise(pv_group.compute_latest(ctx), rust_out, f"rust-batch-min{ti}", tol=max(tol, 1e-6))


def test_step_rust_std_r2_meany_accessors() -> None:
    """std, r2, mean_y via the Rust assemble kernel match the polars assemble path (the accessors price_volume
    lacks) — pins the std (count>1 guard) and r2/mean_y (denom_y guard) arms of ``assemble_canonical``."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    group = _StdR2Group()
    polars_engine = IncrementalEngine([group])
    rust_engine = IncrementalEngine([group])
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        polars_out = polars_engine.step(buffer)[group.name]
        rust_out = rust_engine.step_rust(buffer)[group.name]
        _assert_cellwise(polars_out, rust_out, f"rust-probe-min{minute}", tol=0.0)
