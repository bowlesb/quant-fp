"""Path-excursion range — max draw-up plus max draw-down of the price path over the window.

A price-PATH-shape volatility primitive (family: VOLATILITY, Layer A). Over a trailing window the price
path traces a shape; two scale-free excursion statistics summarize how far it travelled from its own
running extremes:

  * max draw-down ``|maxdd|`` = the deepest the close fell below its running peak, ``-min_t(close_t /
    cummax(close_{start..t}) - 1)`` — a non-negative fraction,
  * max draw-up ``maxdu`` = the highest the close rose above its running trough, ``max_t(close_t /
    cummin(close_{start..t}) - 1)`` — a non-negative fraction.

Their SUM ``draw_range = |maxdd| + maxdu`` is the total peak-to-trough-and-back excursion of the path —
a sign-symmetric realized-path-range measure (a big swing in EITHER direction lifts it), distinct from the
intrabar high-low range (``realized_range``: within-bar) and from close-to-close vol (``volatility``: a
second moment, blind to ordering). It captures path ROUGHNESS / round-trip travel that a variance misses.

The group emits ``draw_range`` (the promoted total) plus its two non-negative legs ``max_drawup`` (max
favorable excursion) and ``max_drawdown`` (max adverse excursion). The legs fall out of the SAME windowed
gather at no extra compute, are standard MFE/MAE path-shape primitives in their own right, and keep the
per-feature cost of this path-dependent (window-anchored cum-extreme) group in the bar-path latency
budget. The directional ASYMMETRY ``|maxdd| - maxdu`` (the screen's weak directional sibling) is NOT
emitted — only the magnitudes and their sign-symmetric sum.

WHY (feature-invention batch 4, experiments/2026-06-19-feature-invention): in the batch-4 forward-IC
screen ``f_draw_range`` carries fwd-realized-vol IC +0.81 (z 267 vs the within-timestamp shuffle floor,
the strongest fwd-RV predictor of the batch) and fwd-VOLUME IC +0.25, stable across the spread days. A
forward VOL/BURST predictor; no directional alpha (|ret IC| <= 0.045, consistent with the portfolio's
direction null — the SUM is sign-symmetric by construction; the asymmetry ``|maxdd|-maxdu`` was the weak
directional sibling and is NOT promoted here).

PARITY (Layer A): the running cum-max / cum-min are anchored at the window's OWN earliest in-window bar
(the excursion at minute T is measured only over the trailing window's bars), so the value at T uses only
bars <= T and is identical regardless of how far the buffer extends past T (no cross-window state). The
live path is ``compute_latest_on_window`` — the IDENTICAL ``compute()`` run on the input sliced to the
trailing window it reads — so live == backfill BY CONSTRUCTION. RT-GREEN (a per-window pass over the
window's closes computing a monotone running max/min then a min/max — O(window); no OLS, no order
statistic on the full buffer). GUARDS: each per-bar drawdown/draw-up guards its running-extreme divisor
``> 0`` (Guard 2 → a degenerate non-positive-price bar contributes null, excluded from the excursion) and
a final ``is_finite()`` backstop converts any stray non-finite to the agreed NULL identically on both
paths. Null on warmup (a window with fewer than two closes has no excursion).
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

WINDOWS: tuple[int, ...] = (60,)
# Slack added to the deepest window for the live window-slice: the excursion over the trailing w minutes
# reads only closes in [T-w, T]; one extra minute is a conservative cushion (the generic parity test fails
# loudly if it were too tight — the documented guard on compute_latest_on_window).
_WINDOW_SLACK = 1
MIN_POINTS = 2  # a path excursion needs at least two closes (one cannot draw up or down)

_OUT_SCHEMA = {
    "symbol": pl.String,
    "minute": pl.Datetime("us", "UTC"),
    **{
        name: pl.Float64
        for w in WINDOWS
        for name in (f"draw_range_{w}m", f"max_drawup_{w}m", f"max_drawdown_{w}m")
    },
}


def _draw_range_window(frame: pl.DataFrame, w: int) -> pl.DataFrame:
    """Window-anchored total path excursion ``|maxdd| + maxdu`` over the trailing ``w`` minutes, point-in-time.

    For each (symbol, minute=T) the trailing window (T-w, T] of closes is reduced in one rolling agg: the
    running cum-max / cum-min are taken over the window's bars (anchored at the window's own earliest bar),
    the per-bar drawdown ``close/run_max - 1`` and draw-up ``close/run_min - 1`` are formed, and their
    window MIN / MAX give ``maxdd`` / ``maxdu``. Because the running extremes restart at the window's first
    bar and never carry buffer-wide state, the value at T uses only bars <= T and is identical however far
    the buffer extends past T. Null where undefined (fewer than MIN_POINTS closes)."""
    col = f"draw_range_{w}m"
    # Aggregate the window-anchored excursions DIRECTLY in the rolling agg (no intermediate list column to
    # materialize twice): for each (symbol, minute=T) over the trailing-w closes, ``cum_max``/``cum_min`` are
    # the running extremes anchored at the window's earliest bar, and the per-bar drawdown
    # ``close/cum_max - 1`` (<= 0) / draw-up ``close/cum_min - 1`` (>= 0) is formed element-wise. We take the
    # window MIN of the drawdown and MAX of the draw-up inside the agg, so only two scalars per window leave
    # the gather (not a per-bar list). A degenerate non-positive-price bar would corrupt cum_max/cum_min, so
    # the input is pre-filtered to close > 0 once before the rolling (Guard 2) — those bars are excluded from
    # the excursion identically on both paths.
    gathered = (
        frame.filter(pl.col("close") > 0.0)
        .rolling(index_column="minute", period=f"{w}m", group_by="symbol")
        .agg(
            (pl.col("close") / pl.col("close").cum_max() - 1.0).min().alias("__maxdd"),
            (pl.col("close") / pl.col("close").cum_min() - 1.0).max().alias("__maxdu"),
            pl.len().alias("__n"),
        )
    )
    warm = pl.col("__n") >= MIN_POINTS
    # The two excursion legs (free from the SAME gather) and their sum. ``max_drawup`` (>= 0) and
    # ``max_drawdown`` (the magnitude |maxdd|, >= 0) are the favorable / adverse running-extreme excursions;
    # ``draw_range`` is their sum (the promoted total round-trip excursion). Each is null on warmup and
    # passed through the is_finite() backstop identically on both paths.
    drawup_leg = pl.when(warm).then(pl.col("__maxdu")).otherwise(None)
    drawdown_leg = pl.when(warm).then(-pl.col("__maxdd")).otherwise(None)
    total = pl.when(warm).then((-pl.col("__maxdd")) + pl.col("__maxdu")).otherwise(None)
    return gathered.select(
        "symbol",
        "minute",
        _finite(total).cast(pl.Float64).alias(col),
        _finite(drawup_leg).cast(pl.Float64).alias(f"max_drawup_{w}m"),
        _finite(drawdown_leg).cast(pl.Float64).alias(f"max_drawdown_{w}m"),
    )


def _finite(expr: pl.Expr) -> pl.Expr:
    """is_finite() backstop: any inf/-inf/nan slipping through becomes NULL identically on both paths."""
    return pl.when(expr.is_finite()).then(expr).otherwise(pl.lit(None, dtype=pl.Float64))


@register
class DrawRangeGroup(FeatureGroup):
    name = "draw_range"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"draw_range_{w}m",
                    description=(
                        f"Total price-path excursion over the trailing {w} minutes: max draw-down "
                        f"(|deepest fall below the running peak|) plus max draw-up (highest rise above the "
                        f"running trough), each a fraction of price. A sign-symmetric realized round-trip "
                        f"range capturing path roughness a variance misses (a forward-vol/burst precursor; "
                        f"batch-4 screen fwd-RV IC +0.81, z 267). Null on a window with fewer than two closes."
                    ),
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"max_drawup_{w}m",
                    description=(
                        f"Max draw-up over the trailing {w} minutes — the highest the close rose above its "
                        f"window-running trough (max favorable excursion), a non-negative fraction of price. "
                        f"The favorable leg of draw_range. Null on a window with fewer than two closes."
                    ),
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"max_drawdown_{w}m",
                    description=(
                        f"Max draw-down magnitude over the trailing {w} minutes — how far the close fell "
                        f"below its window-running peak (max adverse excursion), a non-negative fraction of "
                        f"price. The adverse leg of draw_range. Null on a window with fewer than two closes."
                    ),
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        if frame.height == 0:
            return pl.DataFrame(schema=_OUT_SCHEMA)
        out = frame.select(["symbol", "minute"]).sort(["symbol", "minute"])
        for w in WINDOWS:
            out = out.join(_draw_range_window(frame, w), on=["symbol", "minute"], how="left")
        return out.select(["symbol", "minute", *self.feature_names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Window-sliced live path: the SAME ``compute()`` on the trailing window it reads, filtered to T —
        parity-true by construction (the dropped older minutes cannot affect a window ending at T)."""
        return self.compute_latest_on_window(ctx, max(WINDOWS) + _WINDOW_SLACK)

    def reduce_buffer_minutes(self) -> int:
        return max(WINDOWS) + _WINDOW_SLACK
