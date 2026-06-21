"""Sub-minute inter-arrival burstiness — the trailing mean of the within-minute inter-trade-gap Fano factor.

A trade-arrival burstiness primitive (family: MICROSTRUCTURE, Layer C). Within a single minute the
inter-trade GAP sequence (microseconds between consecutive prints) has its own dispersion: the Fano factor
``var(gaps)/mean(gaps)`` of that gap sequence is high when prints arrive in tight sub-minute clusters
separated by lulls (bursty), near the small-dispersion floor when they are evenly spaced. ``inter_arrival``
already exposes the FAST tail and the rapid-fire fraction of the gaps WITHIN one minute; this is the
SECOND moment of the gap distribution AVERAGED over the trailing window — the persistent sub-minute
burstiness REGIME, not a single minute's tail.

WHY (feature-invention batch 4, experiments/2026-06-19-feature-invention): in the batch-4 forward-IC
screen ``f_subminute_gap_fano`` carries fwd-VOLUME IC −0.67 (z 193 vs the within-timestamp shuffle floor,
the strongest fwd-volume predictor of the batch) — high sub-minute gap-dispersion precedes LOWER forward
volume — orthogonal to the shipped intensity/size features (a timing-shape, not a level). A forward
VOLUME predictor; no directional alpha (|ret IC| <= 0.02, consistent with the portfolio's direction null).

DEFINITION (the screen's ``gap_fano`` averaged over the window): per (symbol, minute) order the prints by
exchange timestamp, take the consecutive inter-trade gaps (the first print of a minute has no gap and is
dropped — no borrowing from the prior minute), and compute the gap Fano ``var(gaps)/mean(gaps)`` (gaps in
microseconds). ``subminute_gap_fano_{w}m`` is the trailing-``w``-minute MEAN of that per-minute gap Fano.

PARITY (Layer C, PARITY_PROMOTION_GATE.md): the per-minute gap Fano is a pure function of THAT minute's
tape ordered by exchange ts (no look-ahead — a cell reads only its own minute's prints, invariant to
receive order), and the windowed mean over the per-minute series is a bounded trailing reduction. The live
path is ``compute_latest_on_window`` — the IDENTICAL ``compute()`` run on the input sliced to the trailing
window it reads — so live == backfill BY CONSTRUCTION (the dropped older minutes cannot influence a window
ending at T). Parity is distributional/tolerance, matching the existing burst families (the exact
microsecond gaps are too tick-order-sensitive for cell-by-cell live-vs-backfill). RT-GREEN (one bounded
group-by over the minute's ticks + a windowed mean; no OLS, no order statistic). GUARDS: the per-minute
Fano guards its denominator ``mean_gap > 0`` (Guard 2 — a mean of non-negative gaps, sign-robust → NULL on
a single-trade / zero-gap minute, never a raw num/denom div-by-zero) and a final ``is_finite()`` backstop
converts any stray non-finite to the agreed NULL identically. A tradeless minute yields no row.
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
from quantlib.features.groups._tick_minute_kernel import (
    per_minute_gap_fano,
    use_rust_tick_minute,
)
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (60,)
# Slack added to the deepest window for the live window-slice: a rolling mean over the trailing w minutes
# reads only minutes in [T-w, T]; one extra minute is a conservative cushion (the generic parity test fails
# loudly if it were too tight — the documented guard on compute_latest_on_window).
_WINDOW_SLACK = 1

_OUT_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    **{f"subminute_gap_fano_{w}m": pl.Float64 for w in WINDOWS},
}


@register
class SubminuteGapFanoGroup(FeatureGroup):
    name = "subminute_gap_fano"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MICROSTRUCTURE
    inputs = (InputSpec(name="trades", columns=("symbol", "ts", "price", "size")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"subminute_gap_fano_{w}m",
                description=(
                    f"Trailing {w}-minute mean of the within-minute inter-trade-gap Fano factor "
                    f"(var(gaps)/mean(gaps) over each minute's consecutive print gaps, in microseconds) — "
                    f"persistent sub-minute arrival burstiness. High = tight bursts of prints separated by "
                    f"lulls (a forward-volume precursor; batch-4 screen fwd-volume IC −0.67, z 193). Null on "
                    f"a window whose minutes all had fewer than two trades."
                ),
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="sparse",
                layer="C",
                parity_method="distributional",
                tolerance=0.10,
            )
            for w in WINDOWS
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        if use_rust_tick_minute():
            # FP_RUST_TICK_MINUTE: the per-minute gap Fano comes from the shared Rust kernel — the SAME
            # var(gaps)/mean(gaps) (ddof=1) over each minute's consecutive print gaps in microseconds,
            # computed with a stable Welford pass (value-identical within the declared tolerance).
            trades = ctx.frame("trades").select(["symbol", "ts", "price", "size"])
            if trades.height == 0:
                return pl.DataFrame(schema=_OUT_SCHEMA)
            per_minute = per_minute_gap_fano(trades).sort(["symbol", "minute"])
        else:
            trades = ctx.frame("trades").select(["symbol", "ts"])
            if trades.height == 0:
                return pl.DataFrame(schema=_OUT_SCHEMA)
            # Order by exchange ts WITHIN each minute, so the first print of a minute has a null gap (no
            # borrowing from the prior minute) and receive order cannot change the result.
            ticks = trades.with_columns(pl.col("ts").dt.truncate("1m").alias("minute"))
            ordered = ticks.sort(["symbol", "minute", "ts"])
            gaps = ordered.with_columns(
                pl.col("ts").diff().over(["symbol", "minute"]).dt.total_microseconds().alias("_gap_us")
            )
            # Per-minute gap Fano = var(gaps)/mean(gaps). The leading null gap (first print of the minute)
            # is dropped; var/mean are over the remaining gaps. ddof=1 var (matches the research screen).
            per_minute = gaps.group_by(["symbol", "minute"]).agg(
                pl.col("_gap_us").drop_nulls().var().alias("_gap_var"),
                pl.col("_gap_us").drop_nulls().mean().alias("_gap_mean"),
            )
            # Guard 2: denominator is a mean of non-negative gaps -> sign-robust; null on a single-trade /
            # zero-gap minute (never a raw num/denom div-by-zero). is_finite() backstop on top.
            fano = (
                pl.when(pl.col("_gap_mean") > 0.0)
                .then(pl.col("_gap_var") / pl.col("_gap_mean"))
                .otherwise(None)
            )
            per_minute = per_minute.with_columns(
                pl.when(fano.is_finite()).then(fano).otherwise(None).alias("_gap_fano")
            ).sort(["symbol", "minute"])
        # Trailing windowed MEAN of the per-minute gap Fano (the bounded reduction). rolling_mean_by skips
        # the per-minute nulls, so a single-trade minute is excluded from the mean on BOTH paths.
        mats = [
            pl.col("_gap_fano")
            .rolling_mean_by("minute", window_size=f"{w}m")
            .over("symbol")
            .alias(f"subminute_gap_fano_{w}m")
            for w in WINDOWS
        ]
        return per_minute.with_columns(mats).select(
            ["symbol", "minute", *[f"subminute_gap_fano_{w}m" for w in WINDOWS]]
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Window-sliced live path: the SAME ``compute()`` on the trailing window it reads, filtered to T —
        parity-true by construction (the dropped older minutes cannot affect a window ending at T)."""
        return self.compute_latest_on_window(ctx, max(WINDOWS) + _WINDOW_SLACK)

    def reduce_buffer_minutes(self) -> int:
        return max(WINDOWS) + _WINDOW_SLACK
