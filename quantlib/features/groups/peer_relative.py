"""Behavioral-peer-relative return — a name's move vs its DATA-DRIVEN peer cluster (family:
CROSS_SECTIONAL).

Most of a name's short-horizon move is shared with the names it co-moves with — its behavioral peer
group. Universe-relative features (breadth, rank, dispersion) demean against the WHOLE universe; this
group demeans against the symbol's OWN behavioral cluster, isolating the idiosyncratic move (the part
not explained by "its peer group is moving"). The cluster map is the #76 SVD co-movement embedding
(11 clusters / 2,722 symbols, cohesion held-out 0.092 vs 0.0003 random — structure real OOS), which
is a DATA-DRIVEN peer grouping (not GICS) — exactly the group that actually co-moves.

Per (symbol, minute) and horizon w:
  ``peer_relative_ret_{w}m`` = ret_{w}(symbol) - mean(ret_{w}) over the symbol's cluster at that minute.

Parity: the cluster assignment is a STATIC nightly lookup (``load_reference`` joins the frozen
``cluster_id`` onto the reference snapshot — identical in stream and backfill, no intraday state). The
intraday compute is a deterministic cross-sectional reduce (group_by (cluster_id, minute), subtract
the cluster mean) that rides the same minute cross-section in both paths -> parity-true by
construction. ``compute_latest`` reruns the same reduce over the FULL buffer and only filters OUTPUT
keys to the latest minute (the breadth ``_assemble`` pattern), so compute_latest == compute().last
(auto-guarded by tests/test_fp_latest.py). Names with a NULL cluster_id (absent from the map) emit
NULL — let the unmapped case be visible, not silently bucketed.
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

PEER_WINDOWS: tuple[int, ...] = (5, 15, 30)


@register
class PeerRelativeReturnGroup(FeatureGroup):
    name = "peer_relative"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (
        InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),
        InputSpec(name="reference", columns=("symbol", "cluster_id")),
    )

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"peer_relative_ret_{window}m",
                description=(
                    f"The symbol's {window}-minute return minus the mean {window}m return of its "
                    f"behavioral-peer cluster at that minute — the idiosyncratic move not explained by "
                    f"its co-movement group (NULL if the symbol has no mapped cluster)."
                ),
                dtype="Float64",
                nan_policy="sparse",
                layer="A",
            )
            for window in PEER_WINDOWS
        ]

    def reduce_buffer_minutes(self) -> int | None:
        """Cross-sectional reduce in the reader's reduce phase; the deepest lookback is the longest
        peer-return horizon."""
        return max(PEER_WINDOWS)

    def _returns(self, ctx: BatchContext) -> pl.DataFrame:
        frame = (
            ctx.frame("minute_agg")
            .select(["symbol", "minute", "close"])
            .sort(["symbol", "minute"])
        )
        for window in PEER_WINDOWS:
            frame = lagged(frame, "close", window, f"_lag{window}")
        return frame.with_columns(
            [
                (pl.col("close") / pl.col(f"_lag{w}") - 1.0).alias(f"_ret_{w}")
                for w in PEER_WINDOWS
            ]
        ).select(["symbol", "minute", *[f"_ret_{w}" for w in PEER_WINDOWS]])

    def _assemble(self, ctx: BatchContext, out_keys: pl.DataFrame) -> pl.DataFrame:
        """The peer-demean over the FULL minute buffer (windowed returns intact); only OUTPUT keys are
        filtered, so compute_latest == compute().last (parity-guarded)."""
        names = [spec.name for spec in self.declare()]
        clusters = ctx.frame("reference").select(["symbol", "cluster_id"])
        returns = self._returns(ctx).join(clusters, on="symbol", how="left")

        peer_mean_exprs = [
            pl.col(f"_ret_{w}")
            .mean()
            .over(["cluster_id", "minute"])
            .alias(f"_peer_{w}")
            for w in PEER_WINDOWS
        ]
        with_peer = returns.with_columns(peer_mean_exprs)
        feat = with_peer.with_columns(
            [
                pl.when(pl.col("cluster_id").is_not_null())
                .then(pl.col(f"_ret_{w}") - pl.col(f"_peer_{w}"))
                .otherwise(None)
                .alias(f"peer_relative_ret_{w}m")
                for w in PEER_WINDOWS
            ]
        ).select(["symbol", "minute", *names])

        return out_keys.join(feat, on=["symbol", "minute"], how="left").select(
            ["symbol", "minute", *names]
        )

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        return self._assemble(ctx, ctx.frame("minute_agg").select(["symbol", "minute"]))

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        keys = ctx.frame("minute_agg").select(["symbol", "minute"])
        latest = keys["minute"].max()
        return self._assemble(ctx, keys.filter(pl.col("minute") == latest))
