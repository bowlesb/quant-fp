"""Parity gates for porting the CROSS-SECTIONAL market groups onto the fast path (this migration).

The decomposition (see the group docstrings):
  * ``market_beta`` is now a ``ReductionGroup``: beta/corr are the windowed-OLS slope/corr of the ticker's
    one-minute return on SPY's one-minute return (a CROSS-SYMBOL BROADCAST regressor), idio_vol = return std
    * sqrt(1-r2). It rides the additive-window paired-sum kernel; the broadcast regressor is sourced by the
    incremental engine's ``broadcast`` StatefulRegressor (read SPY's row, broadcast to the universe).
  * ``market_context`` is a per-minute UNIVERSE GATHER: index trailing returns broadcast + each symbol's own
    trailing return (point lags) - the index, all derived at the latest minute only.

Two invariants, no loosened tolerances:
  1. market_beta: IncrementalEngine.step (the broadcast-regressor fast path) == compute_latest == compute().last,
     cell-for-cell across a minute stream INCLUDING warmup. The broadcast-regressor paired sums must match the
     batch rolling regression on SPY's broadcast return.
  2. market_context: the per-minute gather (compute_latest) == the batch cross-section (compute at that minute),
     cell-for-cell, at every minute including warmup.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.declarative import ReductionGroup
from quantlib.features.groups.market_beta import MarketBetaGroup
from quantlib.features.groups.market_context import MarketContextGroup
from quantlib.features.incremental import IncrementalEngine

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _stream(n_sym: int = 8, n_min: int = 80, seed: int = 11) -> pl.DataFrame:
    """An OHLC minute stream including the SPY + QQQ index symbols (the market-context references). Every
    symbol present every minute — the dense Monday flow the broadcast/gather assume."""
    rng = np.random.default_rng(seed)
    symbols = [f"S{s}" for s in range(n_sym)] + ["SPY", "QQQ"]
    price = {sym: 100.0 + i for i, sym in enumerate(symbols)}
    rows = []
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for sym in symbols:
            price[sym] *= 1.0 + (rng.standard_normal() * 0.003)
            close = price[sym]
            rows.append({"symbol": sym, "minute": minute, "close": close, "volume": 1000.0 + rng.random() * 4000})
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _assert_close(reference: pl.DataFrame, got: pl.DataFrame, label: str, tol: float) -> None:
    assert set(got.columns) == set(reference.columns), f"{label}: columns differ"
    reference = reference.sort("symbol")
    got = got.sort("symbol").select(reference.columns)
    assert got.height == reference.height, f"{label}: row count differs ({got.height} != {reference.height})"
    for col in [c for c in reference.columns if c not in ("symbol", "minute")]:
        joined = reference.select("symbol", col).join(got.select("symbol", pl.col(col).alias("_g")), on="symbol")
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_g").is_null())
                | ((pl.col(col) - pl.col("_g")).abs() <= 1e-9 + tol * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"{label}.{col}: {bad.height} mismatches\n{bad.head()}"


def test_market_beta_incremental_matches_batch_and_backfill() -> None:
    """market_beta fast path: IncrementalEngine.step == compute_latest == compute().last, cell-for-cell across
    the stream incl. warmup. Proves the broadcast-regressor paired sums equal the batch rolling regression on
    SPY's one-minute return (beta/corr/r2 -> idio_vol), at every checkpoint."""
    stream = _stream(n_sym=8, n_min=80)
    minutes = sorted(stream["minute"].unique())
    group = MarketBetaGroup()
    assert isinstance(group, ReductionGroup)
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    backfill = group.compute(BatchContext(frames={"minute_agg": stream}))
    engine = IncrementalEngine([group])

    checkpoints = {12, 16, 31, 46, 61, len(minutes) - 1}  # pre-window-warm through full buffer
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        inc = engine.step(buffer)[group.name]
        if ti in checkpoints:
            ctx = BatchContext(frames={"minute_agg": buffer})
            latest_tol = max(tolerances.values())
            _assert_close(group.compute_latest(ctx), inc, f"min{ti}: incremental==compute_latest", latest_tol)
            back_t = backfill.filter(pl.col("minute") == minute)
            _assert_close(back_t, inc, f"min{ti}: incremental==backfill", latest_tol)


def test_market_context_gather_matches_batch_cross_section() -> None:
    """market_context fast path: the per-minute universe gather (compute_latest) equals the batch cross-section
    (compute at that minute), cell-for-cell, at every minute incl. warmup. Pins the broadcast + own-return
    point-lags against the rolling backfill, so live and backfill rank the IDENTICAL market context."""
    stream = _stream(n_sym=10, n_min=80)
    minutes = sorted(stream["minute"].unique())
    group = MarketContextGroup()
    backfill = group.compute(BatchContext(frames={"minute_agg": stream}))

    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        gather = group.compute_latest(BatchContext(frames={"minute_agg": buffer}))
        back_t = backfill.filter(pl.col("minute") == minute)
        _assert_close(back_t, gather, f"min{ti}: gather==backfill", 1e-9)
