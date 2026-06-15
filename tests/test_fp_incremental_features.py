"""The two LIVE paths agree: IncrementalEngine.step() == per-group compute_latest() (the batch), feature-
for-feature, across a minute stream. Together with test_fp_latest (batch == backfill) this closes the chain
backfill == batch == incremental — the same feature from one declaration, three execution paths."""
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


def _assert_close(batch: pl.DataFrame, inc: pl.DataFrame, label: str) -> None:
    assert set(inc.columns) == set(batch.columns), f"{label}: columns differ"
    batch, inc = batch.sort("symbol"), inc.sort("symbol").select(batch.columns)
    for col in [c for c in batch.columns if c not in ("symbol", "minute")]:
        joined = batch.select("symbol", col).join(inc.select("symbol", pl.col(col).alias("_i")), on="symbol")
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_i").is_null())
                | ((pl.col(col) - pl.col("_i")).abs() <= 1e-6 + 1e-6 * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"{label}.{col}: {bad.height} mismatches\n{bad.head()}"


def test_incremental_step_matches_batch() -> None:
    stream = _stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    engine = IncrementalEngine(groups)

    checkpoints = {10, 30, len(minutes) - 1}  # warmup-ish, mid, full-buffer
    for ti, minute in enumerate(minutes):
        buffer = stream.filter(pl.col("minute") <= minute)
        inc = engine.step(buffer)
        if ti in checkpoints:
            ctx = BatchContext(frames={"minute_agg": buffer})
            for group in groups:
                _assert_close(group.compute_latest(ctx), inc[group.name], f"min{ti}:{group.name}")


def test_slice_derive_matches_whole_buffer() -> None:
    """V2 slice-derive guard: the (n_symbols, n_value_cols) matrix the engine builds for the latest minute —
    short-lag columns over a small slice + stateful regressors (OBV cumulative, time axis) from running state —
    equals the whole-buffer derive (its V1 source of truth), cell-for-cell, at every minute past warmup. This
    pins the slice-derive optimization to the value level (independent of the assemble that follows)."""
    stream = _stream(n_sym=6, n_min=64)
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    engine = IncrementalEngine(groups)
    engine.symbols = sorted(stream["symbol"].unique().to_list())
    engine._seed_stateful(stream)

    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        # whole-buffer derive of the slice-safe columns (V1 path) — the reference for the short-lag columns
        whole = engine._derived_row(buffer, minute)
        whole_safe = whole.select(engine.safe_value_cols).fill_null(0.0).to_numpy()
        sliced = engine._matrix_at(buffer, minute, slice_derive=True)
        for safe_i, col in enumerate(engine.safe_value_cols):
            ref = whole_safe[:, safe_i]
            got = sliced[:, engine.col_index[col]]
            assert np.allclose(ref, got, rtol=1e-9, atol=1e-9), f"{minute} {col}: slice != whole-buffer derive"


def _sparse_stream(n_dense: int = 6, n_min: int = 64, gap: int = 10) -> pl.DataFrame:
    """A dense stream (every symbol every minute) PLUS one sparse symbol ``SP`` that prints only every ``gap``
    minutes (gaps far larger than the legacy DERIVE_SLICE window). At a minute where SP prints, its positional
    prior bar (``close.shift(1).over("symbol")``) is ``gap`` minutes back — a minute-window slice would miss it
    and slice-derive a wrong null lag; the per-symbol row tail reaches it."""
    base = _stream(n_sym=n_dense, n_min=n_min)
    rng = np.random.default_rng(11)
    price = 250.0
    rows = []
    template = base.row(0, named=True)
    for mi in range(0, n_min, gap):
        minute = BASE + dt.timedelta(minutes=mi)
        price *= 1.0 + rng.standard_normal() * 0.003
        row = dict(template)
        row.update({"symbol": "SP", "minute": minute, "open": price * 0.999, "high": price * 1.002,
                    "low": price * 0.998, "close": price, "volume": 2000.0})
        rows.append(row)
    sparse = pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC"))).select(base.columns)
    return pl.concat([base, sparse]).sort(["symbol", "minute"])


def test_slice_derive_sparse_symbol_matches_whole_buffer() -> None:
    """REGRESSION (OPEN PARITY CONSTRAINT, resolved): a symbol that skips minutes still assembles cell-for-cell
    equal to the gap-safe whole-buffer derive. Positional lags need the k-th prior ROW (however far back in
    time), so the slice must tail by ROW per symbol, not by a fixed minute window. Two engines step the same
    sparse stream — one slicing (fast), one whole-buffer (truth); their features must agree at every minute.
    Under the old minute-window slice the sparse symbol's lag columns were a wrong null at its print minutes,
    diverging the running sums — this test would have caught that."""
    stream = _sparse_stream()
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    eng_slice = IncrementalEngine(groups)
    eng_whole = IncrementalEngine(groups)
    assert eng_slice.max_lag >= 1  # the sparse gap (10) must exceed max_lag (and the legacy DERIVE_SLICE) to bite

    sp_minutes = set(stream.filter(pl.col("symbol") == "SP")["minute"].to_list())
    checked_sparse = 0
    for minute in minutes:
        buffer = stream.filter(pl.col("minute") <= minute)
        out_slice = eng_slice.step(buffer, slice_derive=True)
        out_whole = eng_whole.step(buffer, slice_derive=False)
        for group in groups:
            _assert_close(out_whole[group.name], out_slice[group.name], f"{minute}:{group.name}")
        if minute in sp_minutes and minute > min(sp_minutes):
            checked_sparse += 1
    assert checked_sparse >= 3, "test did not exercise enough sparse-symbol print minutes past its first bar"
