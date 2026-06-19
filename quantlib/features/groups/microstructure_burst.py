"""Sub-minute microstructure burst features (family: MICROSTRUCTURE, Layer C).

These draw from RAW TICKS — the precise per-second activity inside a minute — to detect a name
"taking off". Layer C is the hardest parity case (PARITY_PLAYBOOK.md §3): the feature is a pure
function of the tick frame, so live and backfill differ only in the TICKS they were fed, and the
parity test measures exactly that. Rules obeyed here: bucket by the EXCHANGE timestamp (`ts`), not
wall-clock; deterministic ordering (sort by ts); same code live and backfill.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.registry import register


@register
class MicrostructureBurstGroup(FeatureGroup):
    name = "microstructure_burst"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MICROSTRUCTURE
    inputs = (InputSpec(name="trades", columns=("symbol", "ts", "price", "size")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="peak_trades_per_second_1m",
                description="Maximum trades printed in any single second within the minute (peak burst intensity).",
                dtype="Float64",
                valid_range=(0.0, 1e7),
                nan_policy="none",
                layer="C",
                parity_method="tolerance",
            ),
            FeatureSpec(
                name="active_seconds_1m",
                description="Count of distinct seconds within the minute that had at least one trade (0-60).",
                dtype="Float64",
                valid_range=(0.0, 60.0),
                nan_policy="none",
                layer="C",
                parity_method="tolerance",
            ),
            FeatureSpec(
                name="inter_arrival_cv_1m",
                description="Coefficient of variation of inter-trade gaps in the minute (burstiness of arrivals).",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="C",
                # Distributional parity: exact gaps are too tick-order-sensitive to match cell-by-cell,
                # so we confirm the live and settled-backfill DISTRIBUTIONS agree (PARITY_PLAYBOOK §3).
                parity_method="distributional",
                tolerance=0.10,
            ),
            FeatureSpec(
                name="max_runup_1m",
                description="Largest within-minute price run-up: max over trades (in exchange-timestamp order) of price minus the running minimum. A PATH-DEPENDENT pattern feature.",
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="none",
                layer="C",
                parity_method="tolerance",
            ),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        ticks = ctx.frame("trades").select(["symbol", "ts", "price", "size"]).with_columns(
            pl.col("ts").dt.truncate("1m").alias("minute"),
            pl.col("ts").dt.truncate("1s").alias("second"),
        )
        per_second = ticks.group_by(["symbol", "minute", "second"]).agg(pl.len().alias("tps"))
        per_minute = per_second.group_by(["symbol", "minute"]).agg(
            pl.col("tps").max().cast(pl.Float64).alias("peak_trades_per_second_1m"),
            pl.len().cast(pl.Float64).alias("active_seconds_1m"),
        )
        # SORT BY EXCHANGE TIMESTAMP makes both order-sensitive features (gaps, run-up) invariant to
        # the order ticks were *received* — so live (buffer in arrival order) == backfill (tape order).
        ordered = ticks.sort(["symbol", "minute", "ts"])
        gaps = ordered.with_columns(
            pl.col("ts").diff().over(["symbol", "minute"]).dt.total_microseconds().alias("gap_us")
        )
        cv = gaps.group_by(["symbol", "minute"]).agg(
            (pl.col("gap_us").std() / pl.col("gap_us").mean()).cast(pl.Float64).alias("inter_arrival_cv_1m")
        )
        runup = ordered.with_columns(
            (pl.col("price") - pl.col("price").cum_min().over(["symbol", "minute"])).alias("_runup")
        ).group_by(["symbol", "minute"]).agg(pl.col("_runup").max().cast(pl.Float64).alias("max_runup_1m"))
        return (
            per_minute.join(cv, on=["symbol", "minute"], how="left")
            .join(runup, on=["symbol", "minute"], how="left")
            .select(
                ["symbol", "minute", "peak_trades_per_second_1m", "active_seconds_1m",
                 "inter_arrival_cv_1m", "max_runup_1m"]
            )
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Own-minute-only live path: every cell reads ONLY its own minute's tape (seconds/gaps/run-up all
        partitioned ``.over(["symbol", "minute"])``), so the SAME ``compute()`` on the trailing 1-minute tape
        slice (filtered to T) is parity-true by construction — older trades cannot affect T's value. Avoids
        running the per-minute group-by over the whole ~300m trade buffer."""
        return self.compute_latest_on_window(ctx, 1)
