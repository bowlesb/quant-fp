"""Tick run-length / signed-flow features via the Rust kernel (family: MICROSTRUCTURE, Layer C).

Sequential, per-tick, state-machine features over the raw trade tape — the longest consecutive
same-direction run, the run count, and tick-signed volume within each minute. These cannot be
vectorized in Polars (each trade's contribution depends on the running run state left by the prior
trade), so the inner loop lives in Rust (``quant_tick``, in ``rust/``). The SAME kernel is called from
the live tape and the historical backfill through this one group, so parity holds by construction —
and a pure-Python reference pins the Rust output (tests/test_fp_rust.py). Ordering is the total key
(symbol, minute, ts), identical in both feeds, so the order-dependent result is deterministic.
"""
from __future__ import annotations

import polars as pl
import quant_tick

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register

_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    "max_signed_run_1m": pl.Float64,
    "signed_run_count_1m": pl.Float64,
    "tick_signed_volume_1m": pl.Float64,
}


@register
class TickRunLengthGroup(FeatureGroup):
    name = "tick_runlength"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MICROSTRUCTURE
    inputs = (InputSpec(name="trades", columns=("symbol", "ts", "price", "size")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(name="max_signed_run_1m", description="Longest run of consecutive same-direction (tick-rule) trades within the minute — a sequential per-tick burst measure from the raw tape.",
                        dtype="Float64", valid_range=(0.0, 1e7), nan_policy="none", layer="C", parity_method="tolerance"),
            FeatureSpec(name="signed_run_count_1m", description="Number of distinct same-direction tick runs within the minute (how often trade direction flips).",
                        dtype="Float64", valid_range=(0.0, 1e7), nan_policy="none", layer="C", parity_method="tolerance"),
            FeatureSpec(name="tick_signed_volume_1m", description="Sum of tick-rule-signed trade size within the minute (per-tick signed volume from the raw tape).",
                        dtype="Float64", nan_policy="none", layer="C", parity_method="tolerance"),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        trades = ctx.frame("trades").select(["symbol", "ts", "price", "size"])
        if trades.height == 0:
            return pl.DataFrame(schema=_SCHEMA)
        uniq = sorted(trades["symbol"].unique().to_list())
        codes = pl.DataFrame({"symbol": uniq, "_code": list(range(len(uniq)))}, schema={"symbol": pl.String, "_code": pl.Int64})
        trades = (
            trades.join(codes, on="symbol", how="left")
            .with_columns(pl.col("ts").dt.truncate("1m").dt.epoch("s").alias("_min_i"))
            .sort(["_code", "_min_i", "ts"])
        )
        out_code, out_min_i, max_run, run_count, signed_vol = quant_tick.tick_run_features(
            trades["_code"].to_list(), trades["_min_i"].to_list(),
            trades["price"].to_list(), trades["size"].to_list()
        )
        reverse = dict(enumerate(uniq))
        return (
            pl.DataFrame(
                {
                    "symbol": [reverse[c] for c in out_code],
                    "_min_i": out_min_i,
                    "max_signed_run_1m": max_run,
                    "signed_run_count_1m": run_count,
                    "tick_signed_volume_1m": signed_vol,
                }
            )
            .with_columns(((pl.col("_min_i") * 1_000_000).cast(pl.Datetime("us")).dt.replace_time_zone("UTC")).alias("minute"))
            .select(["symbol", "minute", "max_signed_run_1m", "signed_run_count_1m", "tick_signed_volume_1m"])
        )
