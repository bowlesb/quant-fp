"""Shared FP_RUST_TICK_MINUTE fast path for the heavy Layer-C per-minute tape primitives.

``subminute_gap_fano`` derives a per-(symbol, minute) primitive (the within-minute inter-trade-gap Fano
factor) from the raw trade tape and then takes a cheap trailing windowed mean over it. The per-minute step
is the heavy part (sort + per-(symbol, minute) gap diff + var/mean agg over the whole minute's prints,
every minute, for ~7k names). This module moves that step into the ``quant_tick.tick_minute_features``
Rust kernel — ONE stable Welford pass over the minute's prints — marshaled via numpy (zero-copy), not
Python lists.

SCOPE — MEASURED, not assumed. The kernel computes the gap Fano, the notional HHI and the size-bucket
counts together, but only ``subminute_gap_fano`` is wired to it: benchmarking showed the kernel is a clear
win there (~2.3x on both compute() and the live compute_latest at 500 syms x 70 min — its polars path is
genuinely expensive), but a LOSS for ``print_hhi`` and ``size_entropy`` (their polars per-minute group-by
is already cheap, so the per-call numpy marshaling overhead exceeds the compute saved). Those two stay on
polars. The kernel keeps the HHI/bin-count outputs so a future, amortized (compute-once-share-across-groups)
integration can use them without a kernel change.

It is a SPEED path only, gated by ``FP_RUST_TICK_MINUTE``. When the gate is off (default) the group runs
its original polars per-minute group-by — the parity reference. When on, the kernel produces the
value-identical per-minute primitive (the math is the same; only float-summation order differs, within the
declared distributional tolerance — measured max rel diff ~3.5e-16). Because the gate lives in the
per-minute helper that BOTH ``compute()`` and the window-sliced ``compute_latest`` call, live == backfill
by construction.
"""

from __future__ import annotations

import os

import polars as pl
import quant_tick

# Size-entropy bins the kernel histograms into; kept in sync with size_entropy.N_BINS for when the
# entropy group is moved onto the kernel under an amortized integration.
N_SIZE_BUCKETS = 6

_USE_RUST_TICK_MINUTE = (
    bool(os.environ.get("FP_RUST_TICK_MINUTE")) and os.environ.get("FP_RUST_TICK_MINUTE") != "0"
)


def use_rust_tick_minute() -> bool:
    """Whether the FP_RUST_TICK_MINUTE fast path is enabled (read once at import)."""
    return _USE_RUST_TICK_MINUTE


def per_minute_gap_fano(trades: pl.DataFrame) -> pl.DataFrame:
    """Per-(symbol, minute) inter-trade-gap Fano factor via the Rust kernel.

    Returns one row per (symbol, minute) with columns: ``symbol`` (String), ``minute`` (Datetime us UTC),
    ``_gap_fano`` (Float64, null where undefined — < 2 gaps or non-positive mean gap). NaN from the kernel
    is restored to Polars null so the Guard-2 / is_finite semantics match the polars reference exactly.

    ``trades`` must have columns symbol (String), ts (Datetime us), price, size (price/size are read by the
    kernel for its other outputs, which this caller discards).
    """
    uniq = sorted(trades["symbol"].unique().to_list())
    codes = pl.DataFrame(
        {"symbol": uniq, "_code": list(range(len(uniq)))},
        schema={"symbol": pl.String, "_code": pl.Int64},
    )
    ordered = (
        trades.join(codes, on="symbol", how="left")
        .with_columns(
            pl.col("ts").dt.truncate("1m").dt.epoch("us").alias("_min_i"),
            pl.col("ts").dt.epoch("us").alias("_ts_us"),
        )
        .sort(["_code", "_min_i", "_ts_us"])
    )
    out_code, out_min_i, gap_fano, _hhi, _bins = quant_tick.tick_minute_features(
        ordered["_code"].to_numpy(),
        ordered["_min_i"].to_numpy(),
        ordered["_ts_us"].to_numpy(),
        ordered["price"].to_numpy(),
        ordered["size"].to_numpy(),
        N_SIZE_BUCKETS,
    )
    reverse = dict(enumerate(uniq))
    result = pl.DataFrame(
        {
            "symbol": [reverse[code] for code in out_code],
            "_min_i": out_min_i,
            "_gap_fano": gap_fano,
        }
    ).with_columns(
        pl.col("_min_i").cast(pl.Datetime("us")).dt.replace_time_zone("UTC").alias("minute")
    )
    return result.with_columns(
        pl.when(pl.col("_gap_fano").is_finite()).then(pl.col("_gap_fano")).otherwise(None).alias("_gap_fano")
    ).select(["symbol", "minute", "_gap_fano"])
