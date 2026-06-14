"""Volatility features from per-minute bars over many windows (family: VOLATILITY, Layer A).

Realized vol (std of 1-min returns) and Parkinson vol (from the high-low range) over time-anchored
windows — correct on gappy grids.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    BatchContext,
    FeatureGroup,
    FeatureSpec,
    FeatureType,
    InputSpec,
    lagged,
)
from quantlib.features.latest import pivot_stat, rust_reductions
from quantlib.features.registry import register

VOL_WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120)
RANGE_WINDOWS: tuple[int, ...] = (15, 30, 60, 120)
FOUR_LN2 = 2.772588722239781


@register
class VolatilityGroup(FeatureGroup):
    name = "volatility"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "high", "low", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = [
            FeatureSpec(
                name="high_low_range_1m",
                description="Intra-minute high-low range as a fraction of close: (high - low) / close.",
                dtype="Float64",
                valid_range=(0.0, 5.0),
                nan_policy="none",
                layer="A",
            )
        ]
        for w in VOL_WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"realized_vol_{w}m",
                    description=f"Standard deviation of one-minute close-to-close returns over the trailing {w} minutes.",
                    dtype="Float64",
                    valid_range=(0.0, 5.0),
                    nan_policy="warmup",
                    layer="A",
                    tolerance=0.02,
                )
            )
        for w in RANGE_WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"parkinson_vol_{w}m",
                    description=f"Parkinson high-low volatility estimator over the trailing {w} minutes (uses the bar range).",
                    dtype="Float64",
                    valid_range=(0.0, 5.0),
                    nan_policy="warmup",
                    layer="A",
                    tolerance=0.02,
                )
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "high", "low", "close"])
        frame = lagged(frame, "close", 1, "_cp").sort(["symbol", "minute"])
        frame = frame.with_columns(
            [
                (pl.col("close") / pl.col("_cp") - 1.0).alias("_ret"),
                (pl.col("high") / pl.col("low")).log().pow(2).alias("_hl2"),
            ]
        )
        exprs = [((pl.col("high") - pl.col("low")) / pl.col("close")).cast(pl.Float64).alias("high_low_range_1m")]
        for w in VOL_WINDOWS:
            exprs.append(
                pl.col("_ret").rolling_std_by("minute", window_size=f"{w}m").over("symbol").cast(pl.Float64).alias(f"realized_vol_{w}m")
            )
        for w in RANGE_WINDOWS:
            exprs.append(
                (pl.col("_hl2").rolling_mean_by("minute", window_size=f"{w}m").over("symbol") / FOUR_LN2).sqrt().cast(pl.Float64).alias(f"parkinson_vol_{w}m")
            )
        names = ["high_low_range_1m"] + [f"realized_vol_{w}m" for w in VOL_WINDOWS] + [f"parkinson_vol_{w}m" for w in RANGE_WINDOWS]
        return frame.with_columns(exprs).select(["symbol", "minute", *names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """RUST-backed latest-minute form: realized vol = std and Parkinson = sqrt(mean(_hl2)/4ln2),
        both from the Rust windowed_reduce kernel. Parity-guarded to the Polars rolling form within the
        feature's declared 0.02 tolerance (std float-algorithm noise)."""
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "high", "low", "close"])
        frame = lagged(frame, "close", 1, "_cp").sort(["symbol", "minute"])
        frame = frame.with_columns(
            [(pl.col("close") / pl.col("_cp") - 1.0).alias("_ret"), (pl.col("high") / pl.col("low")).log().pow(2).alias("_hl2")]
        )
        realized = pivot_stat(rust_reductions(frame, "_ret", VOL_WINDOWS), "std", "realized_vol_{w}m", VOL_WINDOWS)
        hl = rust_reductions(frame, "_hl2", RANGE_WINDOWS).with_columns((pl.col("mean") / FOUR_LN2).sqrt().alias("_pk"))
        parkinson = pivot_stat(hl, "_pk", "parkinson_vol_{w}m", RANGE_WINDOWS)
        latest = frame["minute"].max()
        current = frame.filter(pl.col("minute") == latest).select(
            ["symbol", ((pl.col("high") - pl.col("low")) / pl.col("close")).cast(pl.Float64).alias("high_low_range_1m")]
        )
        out = current.join(realized, on="symbol", how="left").join(parkinson, on="symbol", how="left").with_columns(pl.lit(latest).alias("minute"))
        names = ["high_low_range_1m"] + [f"realized_vol_{w}m" for w in VOL_WINDOWS] + [f"parkinson_vol_{w}m" for w in RANGE_WINDOWS]
        return out.select(["symbol", "minute", *names])
