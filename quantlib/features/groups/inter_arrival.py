"""Inter-arrival / trade-timing microstructure features from the raw tape (family: MICROSTRUCTURE,
Layer C).

These extend the burst family (``microstructure_burst`` already has ``inter_arrival_cv_1m`` and
``peak_trades_per_second_1m``) with the timing statistics it does NOT cover — the shape of the
inter-trade gap distribution and how evenly trades are spread within the minute:

- ``rapid_fire_ratio_1m`` — fraction of consecutive inter-trade gaps shorter than 100 ms (a HFT /
  burst-of-prints proxy). In [0, 1]; null on a minute with < 2 trades (no gap exists).
- ``p10_inter_arrival_ms_1m`` — the 10th-percentile inter-trade gap in milliseconds (the FAST tail of
  arrivals — small = bursty). Null with < 2 trades.
- ``trade_timing_entropy_1m`` — Shannon entropy (nats) of the trade-count distribution across the 60
  one-second buckets of the minute, normalized to [0, 1] by log(number of active seconds). 1 = activity
  spread evenly across the seconds it touched; toward 0 = clustered into few seconds. Null with 0 trades.

All are pure functions of the minute's tick frame, ordered by the EXCHANGE timestamp (``ts``) so the
result is invariant to receive order — the SAME code runs live and on backfill, so live == backfill by
construction (the parity audit measures only the ticks each was fed). Parity is distributional/tolerance,
matching the existing burst features (PARITY_PLAYBOOK §3). A gap is computed only WITHIN a minute
(``.over(["symbol", "minute"])``), so the first trade of each minute has a null gap and does not borrow
from the prior minute — keeping each cell a function of its own minute only.
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

RAPID_FIRE_MS = 100.0  # an inter-trade gap shorter than this many milliseconds is "rapid fire"

_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    "rapid_fire_ratio_1m": pl.Float64,
    "p10_inter_arrival_ms_1m": pl.Float64,
    "trade_timing_entropy_1m": pl.Float64,
}


@register
class InterArrivalGroup(FeatureGroup):
    name = "inter_arrival"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MICROSTRUCTURE
    inputs = (InputSpec(name="trades", columns=("symbol", "ts", "price", "size")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name="rapid_fire_ratio_1m",
                description=(
                    "Fraction of within-minute inter-trade gaps shorter than 100 ms — a rapid-fire / HFT "
                    "burst proxy. In [0, 1]; null on a minute with fewer than two trades (no gap exists)."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.0),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            ),
            FeatureSpec(
                name="p10_inter_arrival_ms_1m",
                description=(
                    "Tenth-percentile within-minute inter-trade gap in milliseconds (the fast tail of "
                    "arrivals — small means bursty). Null on a minute with fewer than two trades."
                ),
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="C",
                # The percentile of exact gaps is too tick-order-sensitive to match cell-by-cell across
                # live vs settled backfill; confirm the DISTRIBUTIONS agree (like inter_arrival_cv_1m).
                parity_method="distributional",
                tolerance=0.10,
            ),
            FeatureSpec(
                name="trade_timing_entropy_1m",
                description=(
                    "Normalized Shannon entropy of the trade-count distribution across the minute's one-second "
                    "buckets, in [0, 1]: 1 is activity spread evenly over the seconds it touched, near 0 is "
                    "clustered into few seconds. Null on a tradeless minute."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.0001),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            ),
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        trades = ctx.frame("trades").select(["symbol", "ts"])
        if trades.height == 0:
            return pl.DataFrame(schema=_SCHEMA)
        ticks = trades.with_columns(
            pl.col("ts").dt.truncate("1m").alias("minute"),
            pl.col("ts").dt.truncate("1s").alias("second"),
        )
        # Gap stats: order by exchange ts WITHIN each minute, so the first trade of a minute has a null
        # gap (no borrowing from the prior minute) and receive-order cannot change the result.
        ordered = ticks.sort(["symbol", "minute", "ts"])
        gaps = ordered.with_columns(
            pl.col("ts").diff().over(["symbol", "minute"]).dt.total_microseconds().alias("_gap_us")
        )
        gap_stats = gaps.group_by(["symbol", "minute"]).agg(
            # gaps are null on the first tick of each minute → drop_nulls; both null when < 2 trades.
            (pl.col("_gap_us") < RAPID_FIRE_MS * 1000.0).drop_nulls().mean().cast(pl.Float64).alias("rapid_fire_ratio_1m"),
            (pl.col("_gap_us").drop_nulls().quantile(0.10, interpolation="linear") / 1000.0)
            .cast(pl.Float64)
            .alias("p10_inter_arrival_ms_1m"),
        )
        # Timing entropy: count trades per second, then normalized Shannon entropy of that distribution.
        per_second = ticks.group_by(["symbol", "minute", "second"]).agg(pl.len().alias("_cnt"))
        entropy = per_second.group_by(["symbol", "minute"]).agg(
            pl.col("_cnt").sum().alias("_total"),
            pl.len().alias("_active_seconds"),
            # -Σ cnt*ln(cnt) ; combined with total below into -Σ p ln p = ln(total) - (Σ cnt ln cnt)/total.
            (pl.col("_cnt").cast(pl.Float64) * pl.col("_cnt").cast(pl.Float64).log()).sum().alias("_sum_clogc"),
        ).with_columns(
            (
                pl.when((pl.col("_active_seconds") > 1) & (pl.col("_total") > 0))
                .then(
                    (pl.col("_total").cast(pl.Float64).log() - pl.col("_sum_clogc") / pl.col("_total"))
                    / pl.col("_active_seconds").cast(pl.Float64).log()
                )
                # one active second (or single trade) → fully clustered → entropy 0.
                .otherwise(0.0)
            )
            .cast(pl.Float64)
            .alias("trade_timing_entropy_1m")
        )
        return (
            gap_stats.join(
                entropy.select(["symbol", "minute", "trade_timing_entropy_1m"]),
                on=["symbol", "minute"],
                how="full",
                coalesce=True,
            )
            .select(
                ["symbol", "minute", "rapid_fire_ratio_1m", "p10_inter_arrival_ms_1m", "trade_timing_entropy_1m"]
            )
        )
