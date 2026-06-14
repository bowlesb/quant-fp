"""Parity gates for porting liquidity / price_returns / price_levels onto the fast path (this migration).

The decomposition (see the group docstrings):
  * ``liquidity`` is now a ``ReductionGroup``: Amihud = a windowed ``mean``; Roll's autocovariance of
    consecutive price changes = four windowed ``sum`` reductions of the paired columns; Kyle = the windowed
    OLS ``slope`` of the price change on signed volume. It rides the additive-window paired-sum kernel.
  * ``price_returns`` is a ``StatefulGroup`` on the LAG / last-k kind: one ``LagSpec`` per window (close as
    of T − w), with ``ret`` / ``log_ret`` derived in ``assemble``.
  * ``price_levels`` is a ``StatefulGroup`` on the ROLLING-EXTREMA kind: a per-(symbol, window) monotonic
    deque yielding the trailing max-high / min-low, with position/distance derived in ``assemble``.

Two classes of invariant, no loosened tolerances:
  1. KIND invariant for the NEW extrema kind — ``seed(H); fold(m)`` == ``seed(H + m)``, cell-for-cell, and
     both == the Rust ``windowed_reduce`` min/max == polars ``rolling_*_by`` over the present series.
  2. GROUP parity — the live FAST path (IncrementalEngine.step / StatefulEngine.step) == ``compute_latest``
     == ``compute().last``, cell-for-cell, across a minute stream INCLUDING warmup, within each feature's
     declared tolerance.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.declarative import ReductionGroup
from quantlib.features.groups.liquidity import LiquidityGroup
from quantlib.features.groups.price_levels import PriceLevelGroup
from quantlib.features.groups.price_returns import PriceReturnGroup
from quantlib.features.incremental import IncrementalEngine
from quantlib.features.stateful import ExtremaSpec, ExtremaState, StatefulEngine

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _stream(n_sym: int = 8, n_min: int = 90, seed: int = 13) -> pl.DataFrame:
    """An OHLC + volume + signed-volume minute stream (every symbol present every minute — the dense flow)."""
    rng = np.random.default_rng(seed)
    rows = []
    price = {s: 100.0 + s for s in range(n_sym)}
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            price[s] *= 1.0 + (rng.standard_normal() * 0.003)
            close = price[s]
            opn = close * (1.0 + rng.standard_normal() * 0.001)
            high = max(opn, close) * (1.0 + abs(rng.standard_normal()) * 0.001)
            low = min(opn, close) * (1.0 - abs(rng.standard_normal()) * 0.001)
            volume = 1000.0 + rng.random() * 4000.0
            signed = (rng.random() - 0.5) * 2.0 * volume
            rows.append(
                {"symbol": f"S{s}", "minute": minute, "open": opn, "high": high, "low": low,
                 "close": close, "volume": volume, "signed_volume": signed}
            )
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


def test_extrema_state_fold_equals_reseed_and_matches_rolling() -> None:
    """KIND invariant for the rolling-extrema kind: the deque's max-high / min-low at T equals polars
    ``rolling_max_by`` / ``rolling_min_by`` over the present series (the (T-w, T] window), and folding one
    minute == re-seeding with it appended, cell-for-cell."""
    stream = _stream(n_sym=6, n_min=70)
    symbols = sorted(stream["symbol"].unique().to_list())
    windows = (5, 15, 60)
    specs = [ExtremaSpec(alias=f"_hi_{w}", source="high", window=w, op="max") for w in windows]
    specs += [ExtremaSpec(alias=f"_lo_{w}", source="low", window=w, op="min") for w in windows]
    minutes = sorted(stream["minute"].unique())

    def fold_through(upto: int) -> ExtremaState:
        state = ExtremaState(symbols, specs)
        for minute in minutes[: upto + 1]:
            row = stream.filter(pl.col("minute") == minute).sort("symbol")
            sources = {
                "high": row.select(pl.col("high").cast(pl.Float64)).to_numpy().reshape(-1),
                "low": row.select(pl.col("low").cast(pl.Float64)).to_numpy().reshape(-1),
            }
            state.fold(int(minute.timestamp()), sources)
        return state

    # rolling truth at the final minute
    rolling = stream.sort(["symbol", "minute"])
    for w in windows:
        rolling = rolling.with_columns(
            pl.col("high").rolling_max_by("minute", window_size=f"{w}m").over("symbol").alias(f"_hi_{w}"),
            pl.col("low").rolling_min_by("minute", window_size=f"{w}m").over("symbol").alias(f"_lo_{w}"),
        )
    final = fold_through(len(minutes) - 1)
    truth = rolling.filter(pl.col("minute") == minutes[-1]).sort("symbol")
    for w in windows:
        for alias in (f"_hi_{w}", f"_lo_{w}"):
            assert np.allclose(final.extremum(alias), truth[alias].to_numpy(), rtol=1e-12, atol=1e-12), alias

    # fold == reseed at every minute boundary
    for ti in range(1, len(minutes)):
        appended = fold_through(ti)
        incremental = fold_through(ti - 1)
        last = stream.filter(pl.col("minute") == minutes[ti]).sort("symbol")
        incremental.fold(
            int(minutes[ti].timestamp()),
            {
                "high": last.select(pl.col("high").cast(pl.Float64)).to_numpy().reshape(-1),
                "low": last.select(pl.col("low").cast(pl.Float64)).to_numpy().reshape(-1),
            },
        )
        for spec in specs:
            assert np.allclose(
                appended.extremum(spec.alias), incremental.extremum(spec.alias), rtol=1e-12, atol=1e-12, equal_nan=True
            ), f"{spec.alias} @min{ti}"


def test_liquidity_incremental_matches_batch_and_backfill() -> None:
    """liquidity fast path: IncrementalEngine.step == compute_latest == compute().last, cell-for-cell across
    the stream incl. warmup (amihud mean, roll paired-sum autocovariance, kyle OLS slope)."""
    stream = _stream(n_sym=8, n_min=90)
    minutes = sorted(stream["minute"].unique())
    group = LiquidityGroup()
    assert isinstance(group, ReductionGroup)
    tol = max(spec.tolerance for spec in group.declare())
    backfill = group.compute(BatchContext(frames={"minute_agg": stream}))
    engine = IncrementalEngine([group])

    checkpoints = {3, 11, 16, 31, 61, len(minutes) - 1}
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        inc = engine.step(buffer)[group.name]
        if ti in checkpoints:
            ctx = BatchContext(frames={"minute_agg": buffer})
            _assert_close(group.compute_latest(ctx), inc, f"liquidity min{ti}: incremental==compute_latest", tol)
            back_t = backfill.filter(pl.col("minute") == minute)
            _assert_close(back_t, inc, f"liquidity min{ti}: incremental==backfill", tol)


def _stateful_group_parity(group, stream: pl.DataFrame, label: str) -> None:
    minutes = sorted(stream["minute"].unique())
    engine = StatefulEngine(group)
    backfill = group.compute(BatchContext(frames={"minute_agg": stream}))
    tol = max(spec.tolerance for spec in group.declare())
    checkpoints = {1, 6, 16, 31, 61, len(minutes) - 1}
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        ctx = BatchContext(frames={"minute_agg": buffer})
        live_fast = engine.step(buffer, ctx)
        if ti in checkpoints:
            _assert_close(group.compute_latest(ctx), live_fast, f"{label} min{ti}: engine==compute_latest", tol)
            back_t = backfill.filter(pl.col("minute") == minute)
            _assert_close(back_t, live_fast, f"{label} min{ti}: engine==backfill", tol)


def test_price_returns_stateful_parity() -> None:
    _stateful_group_parity(PriceReturnGroup(), _stream(n_sym=8, n_min=90), "price_returns")


def test_price_levels_stateful_parity() -> None:
    _stateful_group_parity(PriceLevelGroup(), _stream(n_sym=8, n_min=90), "price_levels")
