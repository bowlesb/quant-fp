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
