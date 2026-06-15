"""Unified-emit parity gate: ``IncrementalEngine.step_rust_unified()`` — which assembles EVERY reduction
group's features in ONE shared wide-frame pass (``emit_rust_unified``) instead of one per-group polars
frame-build + ``assemble`` each — equals the per-group ``step_rust()`` / ``step_numpy()`` / polars
``step()`` AND the batch ``compute_latest()``, cell-for-cell, FOR EVERY reduction group, across a minute
stream (incl. warmup nulls).

This is the gate the unified-emit scheduling change must hold: a faster emit that changes ANY value is a
FAILURE. Unlike test_fp_incremental_emit (which pins the price_volume accessor surface), this asserts the
SINGLE-PASS unified frame produces byte-identical output to the certified per-group emit for ALL ~13
reduction groups at once — the exact thing the consolidation could break (column collisions, point-column
dedup, per-group slicing).
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


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
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _assert_cellwise(reference: pl.DataFrame, candidate: pl.DataFrame, label: str, tol: float = 0.0) -> None:
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


def test_unified_matches_per_group_rust_every_group() -> None:
    """The single-pass unified emit equals the per-group Rust emit for EVERY reduction group, every minute.
    Two engines fed the identical stream — only the emit scheduling differs — so any divergence (a column
    collision, a botched point dedup, a wrong per-group slice) is the unified emit's, at tol 0 (IDENTICAL)."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    per_group_engine = IncrementalEngine(groups)
    unified_engine = IncrementalEngine(groups)
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        per_group = per_group_engine.step_rust(buffer)
        unified = unified_engine.step_rust_unified(buffer)
        assert set(per_group) == set(unified), f"min{minute}: group set differs"
        for name in per_group:
            _assert_cellwise(per_group[name], unified[name], f"{name}-min{minute}", tol=0.0)


def test_unified_matches_polars_step_every_group() -> None:
    """The single-pass unified emit equals the polars ``assemble_from_long`` path (``step``) for EVERY group,
    every minute — closing unified == polars-assemble cell-for-cell (tol 0: same sums, same algebra)."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    polars_engine = IncrementalEngine(groups)
    unified_engine = IncrementalEngine(groups)
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        polars_out = polars_engine.step(buffer)
        unified = unified_engine.step_rust_unified(buffer)
        for name in polars_out:
            _assert_cellwise(polars_out[name], unified[name], f"{name}-min{minute}", tol=0.0)


def test_unified_matches_batch_every_group() -> None:
    """The single-pass unified emit equals each group's batch ``compute_latest`` (the live source of truth)
    at warmup/mid/full-buffer checkpoints — closing backfill==batch==incremental(unified-emit) for ALL
    reduction groups, the gate the unified scheduling change must hold."""
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    engine = IncrementalEngine(groups)
    checkpoints = {10, 30, len(minutes) - 1}
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        unified = engine.step_rust_unified(buffer)
        if ti in checkpoints:
            ctx = BatchContext(frames={"minute_agg": buffer})
            for group in groups:
                tol = max(spec.tolerance for spec in group.declare())
                _assert_cellwise(
                    group.compute_latest(ctx), unified[group.name], f"{group.name}-batch-min{ti}",
                    tol=max(tol, 1e-6),
                )
