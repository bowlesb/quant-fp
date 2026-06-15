"""Incremental engine == batch under a FLUCTUATING active symbol set — the live-capture regime.

Live capture delivers only the minute's ACTIVE symbols (a changing subset of the fixed session universe),
not every symbol every minute. The accumulator's windowed sums must still match the batch recompute: a
symbol absent in a minute has NO bar in the batch (no row -> no contribution), and the incremental path
must reproduce that by folding a ZERO contribution for it AND masking it out of the OLS pairing. These
tests pin that — first at the raw windowed-sum level, then end-to-end over the real declarative groups.

Together with the fixed-set tests (test_fp_incremental*, test_fp_latest) this extends the parity chain
backfill == batch == incremental to the membership-churn case that blocked live integration. Parity is
sacred (CLAUDE.md): the absent-symbol-as-zero identity is proven here cell-for-cell, not assumed.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.compare import runnable
from quantlib.features.declarative import ReductionGroup
from quantlib.features.incremental import IncrementalEngine, WindowedSumState
from quantlib.features.latest import rust_windowed_sums

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)


def test_windowed_sum_absent_symbol_equals_missing_row() -> None:
    """The foundational identity: folding a 0 row for an absent symbol == omitting its row from the batch.
    Drives every symbol present at minute 0 (warmup), then drops symbols at random per minute; the running
    sums (absent -> zero contribution) match ``rust_windowed_sums`` over the frame with those rows removed."""
    n_sym, n_min, n_cols = 40, 80, 3
    windows = (5, 10, 20, 45)
    symbols = [f"S{i:03d}" for i in range(n_sym)]
    rng = np.random.default_rng(11)

    mats = [rng.standard_normal((n_sym, n_cols)) for _ in range(n_min)]
    # presence[m, s]: minute 0 fully present; thereafter each symbol present with prob 0.7
    presence = rng.random((n_min, n_sym)) < 0.7
    presence[0, :] = True
    minutes = [BASE + dt.timedelta(minutes=i) for i in range(n_min)]
    epochs = [int(m.timestamp()) for m in minutes]

    # batch frame: only the (minute, symbol) cells that are PRESENT
    rows = []
    for mi in range(n_min):
        for si in range(n_sym):
            if presence[mi, si]:
                rows.append({"symbol": symbols[si], "minute": minutes[mi],
                             "v0": mats[mi][si, 0], "v1": mats[mi][si, 1], "v2": mats[mi][si, 2]})
    frame = pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC"))).sort(["symbol", "minute"])
    batch = rust_windowed_sums(frame, ["v0", "v1", "v2"], windows)

    # incremental: full (n_sym, n_cols) matrix each minute, absent rows zeroed (the alignment live capture does)
    state = WindowedSumState(symbols, windows, n_cols)
    for mi in range(n_min):
        minute_mat = np.where(presence[mi][:, None], mats[mi], 0.0)
        state.update(epochs[mi], minute_mat)
        state.trim()

    for w in windows:
        inc = state.sums(w)
        sub = batch.filter(pl.col("window") == w)
        ref = {s: (a, b, c) for s, a, b, c in zip(sub["symbol"], sub["v0"], sub["v1"], sub["v2"])}
        for si, sym in enumerate(symbols):
            expect = ref.get(sym, (0.0, 0.0, 0.0))  # a symbol with no rows in the window sums to zero
            for ci in range(n_cols):
                assert abs(inc[si, ci] - expect[ci]) <= 1e-9, f"w{w} {sym} c{ci}: {inc[si, ci]} vs {expect[ci]}"


def _fluctuating_stream(n_sym: int, n_min: int, present_p: float, seed: int) -> pl.DataFrame:
    """A bar stream where minute 0 has every symbol (clean warmup) and each later minute carries a random
    ~``present_p`` subset — the live-capture membership-churn shape. One contiguous price walk per symbol
    across the minutes it IS present (so returns/products are well-defined over its present bars)."""
    rng = np.random.default_rng(seed)
    price = {s: 100.0 + s for s in range(n_sym)}
    rows = []
    for mi in range(n_min):
        minute = BASE + dt.timedelta(minutes=mi)
        for s in range(n_sym):
            present = mi == 0 or rng.random() < present_p
            price[s] *= 1.0 + (rng.standard_normal() * 0.002)
            if not present:
                continue
            c = price[s]
            rows.append({"symbol": f"S{s}", "minute": minute, "open": c * 0.999, "high": c * 1.002,
                         "low": c * 0.998, "close": c, "volume": 1000.0 + rng.random() * 4000,
                         "n_trades": float(rng.integers(1, 200)), "signed_volume": rng.standard_normal() * 1000,
                         "mean_spread_bps": rng.random() * 5, "quote_imbalance": rng.standard_normal() * 0.3,
                         "mean_bid_size": rng.random() * 100, "mean_ask_size": rng.random() * 100})
    return pl.DataFrame(rows).with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))


def _assert_close_on_shared(batch: pl.DataFrame, inc: pl.DataFrame, label: str) -> None:
    """Cell-for-cell on the symbols in BOTH frames (the batch emits the latest minute's present symbols; the
    incremental holds running sums for the whole index). Every batch symbol must appear in the incremental."""
    assert set(inc.columns) == set(batch.columns), f"{label}: columns differ"
    shared = set(batch["symbol"].to_list()) & set(inc["symbol"].to_list())
    assert shared == set(batch["symbol"].to_list()), f"{label}: incremental missing batch symbols"
    batch = batch.filter(pl.col("symbol").is_in(list(shared))).sort("symbol")
    inc = inc.filter(pl.col("symbol").is_in(list(shared))).sort("symbol").select(batch.columns)
    for col in [c for c in batch.columns if c not in ("symbol", "minute")]:
        joined = batch.select("symbol", col).join(inc.select("symbol", pl.col(col).alias("_i")), on="symbol")
        bad = joined.filter(
            ~(
                (pl.col(col).is_null() & pl.col("_i").is_null())
                | ((pl.col(col) - pl.col("_i")).abs() <= 1e-6 + 1e-6 * pl.col(col).abs())
            )
        )
        assert bad.height == 0, f"{label}.{col}: {bad.height} mismatches\n{bad.head()}"


def test_incremental_step_matches_batch_under_membership_churn() -> None:
    """End-to-end: with the index pinned to the full universe and ~70% of symbols present each minute, the
    incremental ``step`` features equal the batch ``compute_latest`` for every symbol active at the mark.
    Whole-buffer derive (``slice_derive=False``) so the per-minute value columns are gap-safe (a sparse
    symbol's prior bar may be many minutes back); the membership ALIGNMENT — absent -> zero sum, present-mask
    on the OLS — is what's under test here, independent of the fast slice's density assumption."""
    stream = _fluctuating_stream(n_sym=10, n_min=72, present_p=0.7, seed=7)
    universe = sorted(stream["symbol"].unique().to_list())
    minutes = sorted(stream["minute"].unique())
    groups = [g for g in runnable({"minute_agg": stream}) if isinstance(g, ReductionGroup)]
    assert groups, "expected declarative reduction groups to run on the stream"

    engine = IncrementalEngine(groups)
    engine.seed(stream.filter(pl.col("minute") == minutes[0]), symbols=universe, slice_derive=False)

    checkpoints = {35, 55, len(minutes) - 1}
    for ti, minute in enumerate(minutes[1:], start=1):
        buffer = stream.filter(pl.col("minute") <= minute)
        inc = engine.step(buffer, slice_derive=False)
        if ti in checkpoints:
            ctx = BatchContext(frames={"minute_agg": buffer})
            for group in groups:
                _assert_close_on_shared(group.compute_latest(ctx), inc[group.name], f"min{ti}:{group.name}")
