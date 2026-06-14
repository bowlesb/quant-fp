"""Parity gates for the per-symbol stateful KINDS (recursive EMA, lag/last-k) and the two groups ported onto
them (technical, candlestick). Two invariants per the abstraction (docs/STATE_ABSTRACTION.md):

  1. KIND invariant — ``seed(H); fold(m)`` == ``seed(H + m)``, cell-for-cell: folding one minute equals
     re-seeding with it appended. This is what makes the live fold parity-true.
  2. GROUP parity — the live FAST path (``StatefulEngine.step``) == ``compute_latest`` == ``compute().last``
     (the certified backfill rolling form), cell-for-cell across a minute stream INCLUDING warmup. So the same
     declaration runs three ways and agrees: backfill == batch == incremental.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.groups.candlestick import CandlestickGroup
from quantlib.features.groups.technical import TechnicalGroup
from quantlib.features.stateful import EMASpec, EMAState, LagSpec, LastKState, StatefulEngine

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def _stream(n_sym: int = 8, n_min: int = 80, seed: int = 7) -> pl.DataFrame:
    """An OHLC minute stream (every symbol present every minute — the dense Monday flow)."""
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
            rows.append({"symbol": f"S{s}", "minute": minute, "open": opn, "high": high, "low": low, "close": close})
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _assert_close(reference: pl.DataFrame, got: pl.DataFrame, label: str, tol: float = 1e-6) -> None:
    assert set(got.columns) == set(reference.columns), f"{label}: columns differ"
    reference = reference.sort("symbol")
    got = got.sort("symbol").select(reference.columns)
    assert got.height == reference.height, f"{label}: row count differs"
    for col in [c for c in reference.columns if c not in ("symbol", "minute")]:
        joined = reference.select("symbol", col).join(got.select("symbol", pl.col(col).alias("_g")), on="symbol")
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_g").is_null())
                | ((pl.col(col) - pl.col("_g")).abs() <= 1e-9 + tol * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"{label}.{col}: {bad.height} mismatches\n{bad.head()}"


def test_ema_state_fold_equals_reseed() -> None:
    """KIND invariant for the recursive (EMA) kind, including a CHAINED EMA (the macd-signal shape): fold one
    minute == re-seed with it appended, plus both == polars ``ewm_mean`` over the present series."""
    stream = _stream(n_sym=5, n_min=40)
    symbols = sorted(stream["symbol"].unique().to_list())
    specs = [
        EMASpec(alias="e12", span=12, source="close"),
        EMASpec(alias="e26", span=26, source="close"),
        EMASpec(alias="sig", span=9, combine=lambda emitted, src: emitted["e12"] - emitted["e26"],
                rolling=pl.col("e12") - pl.col("e26")),
    ]
    minutes = sorted(stream["minute"].unique())

    def fold_through(upto: int) -> EMAState:
        state = EMAState(symbols, specs)
        for minute in minutes[: upto + 1]:
            row = stream.filter(pl.col("minute") == minute).sort("symbol")
            close = row.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1)
            state.fold({"close": close})
        return state

    # fold(H + m) reached two ways must agree (the seed==fold invariant), at every minute.
    for ti in range(1, len(minutes)):
        appended = fold_through(ti)  # seed over H+m in one pass
        incremental = fold_through(ti - 1)  # seed over H ...
        last = stream.filter(pl.col("minute") == minutes[ti]).sort("symbol")
        incremental.fold({"close": last.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1)})  # ... then fold m
        for alias in ("e12", "e26", "sig"):
            assert np.allclose(appended.ema(alias), incremental.ema(alias), rtol=1e-12, atol=1e-12), f"{alias} @min{ti}"

    # and the folded EMAs equal polars ewm_mean over the present series, cell-for-cell at T.
    final = fold_through(len(minutes) - 1)
    rolling = (
        stream.sort(["symbol", "minute"])
        .with_columns(
            pl.col("close").ewm_mean(span=12).over("symbol").alias("e12"),
            pl.col("close").ewm_mean(span=26).over("symbol").alias("e26"),
        )
        .with_columns((pl.col("e12") - pl.col("e26")).ewm_mean(span=9).over("symbol").alias("sig"))
        .filter(pl.col("minute") == minutes[-1])
        .sort("symbol")
    )
    for alias in ("e12", "e26", "sig"):
        ref = rolling[alias].to_numpy()
        assert np.allclose(final.ema(alias), ref, rtol=1e-9, atol=1e-9), f"{alias} != polars ewm"


def test_lag_state_fold_equals_reseed() -> None:
    """KIND invariant for the lag/last-k kind: the ring's ``lag()`` at T equals a TIME-based self-join (the
    ``base.lagged`` contract), and folding one minute == re-seeding with it appended."""
    stream = _stream(n_sym=4, n_min=30)
    symbols = sorted(stream["symbol"].unique().to_list())
    specs = [LagSpec(alias="prev_close", source="close", minutes=1)]
    minutes = sorted(stream["minute"].unique())

    state = LastKState(symbols, specs)
    for minute in minutes:
        row = stream.filter(pl.col("minute") == minute).sort("symbol")
        close = row.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1)
        state.fold(int(minute.timestamp()), {"close": close})
    # prev_close at the last minute == close at the previous minute, per symbol.
    prev_row = stream.filter(pl.col("minute") == minutes[-2]).sort("symbol")
    expected = prev_row.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1)
    assert np.allclose(state.lag("prev_close"), expected, rtol=1e-12, atol=1e-12)

    # fold == reseed: re-seed over the first k minutes, fold the next, compare to a single seed over k+1.
    k = 20
    reseed = LastKState(symbols, specs)
    for minute in minutes[: k + 1]:
        row = stream.filter(pl.col("minute") == minute).sort("symbol")
        reseed.fold(int(minute.timestamp()), {"close": row.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1)})
    stepwise = LastKState(symbols, specs)
    for minute in minutes[:k]:
        row = stream.filter(pl.col("minute") == minute).sort("symbol")
        stepwise.fold(int(minute.timestamp()), {"close": row.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1)})
    last = stream.filter(pl.col("minute") == minutes[k]).sort("symbol")
    stepwise.fold(int(minutes[k].timestamp()), {"close": last.select(pl.col("close").cast(pl.Float64)).to_numpy().reshape(-1)})
    assert np.allclose(stepwise.lag("prev_close"), reseed.lag("prev_close"), rtol=1e-12, atol=1e-12, equal_nan=True)


def _group_parity(group, stream: pl.DataFrame, label: str) -> None:
    """Engine.step == compute_latest == compute().last, cell-for-cell, across the minute stream incl. warmup."""
    minutes = sorted(stream["minute"].unique())
    engine = StatefulEngine(group)
    backfill = group.compute(BatchContext(frames={"minute_agg": stream}))
    checkpoints = {1, 5, 12, 26, 40, len(minutes) - 1}  # warmup (pre-EMA-warm) through full buffer
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        ctx = BatchContext(frames={"minute_agg": buffer})
        live_fast = engine.step(buffer, ctx)
        if ti in checkpoints:
            certified = group.compute_latest(ctx)
            _assert_close(certified, live_fast, f"{label} min{ti}: engine==compute_latest")
            back_t = backfill.filter(pl.col("minute") == minute)
            _assert_close(back_t, live_fast, f"{label} min{ti}: engine==backfill")


def test_technical_stateful_parity() -> None:
    _group_parity(TechnicalGroup(), _stream(n_sym=8, n_min=80), "technical")


def test_candlestick_stateful_parity() -> None:
    _group_parity(CandlestickGroup(), _stream(n_sym=8, n_min=80), "candlestick")
