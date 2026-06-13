"""Market-context features: every ticker's minute vector carries the broad-market state (family:
CROSS_SECTIONAL, Layer A).

SPY and QQQ are ingested as ordinary subscribed symbols (no new data path), so their bars sit in the
same ``minute_agg`` as everything else. This group computes each index's trailing returns ONCE and
broadcasts them to every (symbol, minute) by a minute join, then derives each ticker's return
relative to SPY. Because both the live buffer and the settled backfill feed the identical minute_agg
through this identical join, the broadcast is parity-true by construction.

VIX-based features are intentionally omitted: Alpaca does not carry the VIX index cleanly, and a VIXY
ETF proxy is a different series — adding it would need its own source + parity story (bucket B).
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
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 45, 60, 90, 120)
INDICES: dict[str, str] = {"market": "SPY", "nasdaq": "QQQ"}  # feature prefix -> ticker
RELATIVE_INDEX = "market"  # relative performance is measured against SPY


@register
class MarketContextGroup(FeatureGroup):
    name = "market_context"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            for prefix, ticker in INDICES.items():
                specs.append(
                    FeatureSpec(name=f"{prefix}_return_{w}m", description=f"Trailing {w}-minute close-to-close return of the {ticker} index, broadcast to every ticker as of the minute open.",
                                dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="sparse", layer="A")
                )
            specs.append(
                FeatureSpec(name=f"relative_return_{w}m", description=f"This ticker's trailing {w}-minute return minus SPY's over the same window (market-relative excess return).",
                            dtype="Float64", valid_range=(-6.0, 6.0), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"outperforming_{w}m", description=f"1.0 when this ticker's trailing {w}-minute return exceeds SPY's over the same window, else 0.0.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"])
        for w in WINDOWS:
            frame = lagged(frame, "close", w, f"_lag{w}")
        frame = frame.sort(["symbol", "minute"])
        own = frame.with_columns(
            [(pl.col("close") / pl.col(f"_lag{w}") - 1.0).alias(f"_own_{w}") for w in WINDOWS]
        )

        market = own.select("minute").unique().sort("minute")
        for prefix, ticker in INDICES.items():
            index_returns = own.filter(pl.col("symbol") == ticker).select(
                ["minute", *[(pl.col(f"_own_{w}")).alias(f"{prefix}_return_{w}m") for w in WINDOWS]]
            )
            market = market.join(index_returns, on="minute", how="left")

        out = own.select(["symbol", "minute", *[f"_own_{w}" for w in WINDOWS]]).join(market, on="minute", how="left")
        exprs = []
        for w in WINDOWS:
            for prefix in INDICES:
                exprs.append(pl.col(f"{prefix}_return_{w}m").cast(pl.Float64).alias(f"{prefix}_return_{w}m"))
            rel = pl.col(f"_own_{w}") - pl.col(f"{RELATIVE_INDEX}_return_{w}m")
            exprs.append(rel.cast(pl.Float64).alias(f"relative_return_{w}m"))
            exprs.append((rel > 0.0).cast(pl.Float64).alias(f"outperforming_{w}m"))
        names = [spec.name for spec in self.declare()]
        return out.with_columns(exprs).select(["symbol", "minute", *names])
