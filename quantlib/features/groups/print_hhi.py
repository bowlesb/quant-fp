"""Within-minute print-concentration (Herfindahl) of the trade tape (family: MICROSTRUCTURE, Layer C).

WHY (feature-invention batch 3, experiments/2026-06-19-feature-invention): how CONCENTRATED a minute's
traded notional is in a few prints — a Herfindahl index over the minute's individual trades — is a
distinct microstructure channel that screen-predicts the validated vol-burst label (``|fwd ret| >= 2%``
over 5/20/30m, the same target ``realized_range`` + ``large_print_burst`` were promoted on). In the
batch-3 screen ``f_print_hhi`` carries AUC-0.5 −0.20 on burst_5m (z 12-27), is stable-sign across all
three horizons (−,−,−), and has fwd-vol IC −0.52 (z 180). A LOW Herfindahl (notional spread evenly over
many prints) precedes a burst — broad participation, not one block, is the precursor here — which is the
HEAD of a real "concentration" channel orthogonal to the already-shipped vol-burst features and to the
size-entropy / range-expansion channels of the same batch (cross-feature within-ts |IC| max ~0.55).

DEFINITION (the screen's ``hhi_min`` averaged over the window): per (symbol, minute) the within-minute
notional Herfindahl is ``Σ_i notional_i² / (Σ_i notional_i)²`` over the minute's prints
(``notional_i = price_i · size_i``); in [1/n_trades, 1] — near 0 = evenly spread across many prints,
1 = a single print. ``print_hhi_{w}m`` is the trailing-``w``-minute MEAN of that per-minute Herfindahl.

PARITY (Layer C, PARITY_PROMOTION_GATE.md): the per-minute Herfindahl is a pure function of THAT
minute's tape (no look-ahead — a cell reads only its own minute's prints), and the windowed mean over
the per-minute series is a bounded trailing reduction. The live path is ``compute_latest_on_window`` —
the IDENTICAL ``compute()`` run on the input sliced to the trailing window it reads — so live ==
backfill BY CONSTRUCTION (the dropped older bars cannot influence a window ending at T). RT-GREEN (one
bounded group-by over the minute's ticks + a windowed mean; no OLS, no order statistic). GUARDS: the
per-minute Herfindahl guards its denominator ``(Σnotional)² > 0`` (Guard 2 — a square of a non-negative
sum, sign-robust → NULL on a zero-notional minute, never a raw num/denom div-by-zero), and a final
``is_finite()`` backstop converts any stray non-finite to the agreed NULL identically on both paths. A
tradeless minute yields no row (the honest "no trades", not a fabricated zero).
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

WINDOWS: tuple[int, ...] = (30, 60)
# Slack added to the deepest window for the live window-slice: a rolling mean over the trailing w
# minutes reads only bars in [T-w, T]; one extra minute of slack is a conservative cushion (the generic
# parity test fails loudly if it were too tight — the documented guard on compute_latest_on_window).
_WINDOW_SLACK = 1

_OUT_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    **{f"print_hhi_{w}m": pl.Float64 for w in WINDOWS},
}


@register
class PrintHHIGroup(FeatureGroup):
    name = "print_hhi"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MICROSTRUCTURE
    inputs = (InputSpec(name="trades", columns=("symbol", "ts", "price", "size")),)

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"print_hhi_{w}m",
                description=(
                    f"Trailing {w}-minute mean of the within-minute notional Herfindahl "
                    f"(Σ notional_i² / (Σ notional_i)² over the minute's prints) — how concentrated traded "
                    f"notional is in a few prints. Near 0 = spread evenly over many prints (broad "
                    f"participation, a vol-burst precursor; batch-3 screen z 12-27, fwd-vol IC −0.52), "
                    f"1 = a single print. Null on a window with no trades."
                ),
                dtype="Float64",
                valid_range=(0.0, 1.0001),
                nan_policy="sparse",
                layer="C",
                parity_method="tolerance",
            )
            for w in WINDOWS
        ]

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        trades = ctx.frame("trades").select(["symbol", "ts", "price", "size"])
        if trades.height == 0:
            return pl.DataFrame(schema=_OUT_SCHEMA)
        # Per (symbol, minute) within-minute notional Herfindahl — a pure function of that minute's tape.
        per_trade = trades.with_columns(
            pl.col("ts").dt.truncate("1m").alias("minute"),
            (pl.col("price") * pl.col("size")).alias("_notional"),
        )
        per_minute = per_trade.group_by(["symbol", "minute"]).agg(
            pl.col("_notional").pow(2).sum().alias("_sumsq"),
            pl.col("_notional").sum().alias("_sum"),
        )
        # Guard 2: denominator is the SQUARE of a non-negative sum -> sign-robust; null on a zero-notional
        # minute (never a raw num/denom div-by-zero). is_finite() backstop on top.
        hhi = (
            pl.when(pl.col("_sum") > 0.0)
            .then(pl.col("_sumsq") / pl.col("_sum").pow(2))
            .otherwise(None)
        )
        per_minute = per_minute.with_columns(
            pl.when(hhi.is_finite()).then(hhi).otherwise(None).alias("_hhi")
        ).sort(["symbol", "minute"])
        # Trailing windowed MEAN of the per-minute Herfindahl (the bounded reduction). rolling_mean_by
        # skips the per-minute nulls, so a zero-notional minute is excluded from the mean on BOTH paths.
        mats = [
            pl.col("_hhi")
            .rolling_mean_by("minute", window_size=f"{w}m")
            .over("symbol")
            .alias(f"print_hhi_{w}m")
            for w in WINDOWS
        ]
        return per_minute.with_columns(mats).select(
            ["symbol", "minute", *[f"print_hhi_{w}m" for w in WINDOWS]]
        )

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Window-sliced live path: the SAME ``compute()`` on the trailing window it reads, filtered to T —
        parity-true by construction (the dropped older minutes cannot affect a window ending at T)."""
        return self.compute_latest_on_window(ctx, max(WINDOWS) + _WINDOW_SLACK)

    def reduce_buffer_minutes(self) -> int:
        return max(WINDOWS) + _WINDOW_SLACK
