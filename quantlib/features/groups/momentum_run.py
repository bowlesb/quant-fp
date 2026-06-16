"""Momentum run/shape features (family: TREND_QUALITY, Layer A).

Two momentum-quality shapes beyond the second-moment residual spread:

  * ``residual_skew_{W}m`` — skewness of the OLS residuals around the close-vs-time trend. The third moment is
    a closed form of power sums up to THIRD order of the time axis and close (``Σx^3, Σx^2·y, Σx·y^2, Σy^3``
    plus the lower orders), so it is computed VECTORIZED via polars ``rolling_sum_by`` over those product
    columns — one pass, no per-row fit. Centering the time axis on the frame's earliest minute keeps the sums
    well-conditioned; the residual moments are origin-invariant. (The platform OLS reduction kernel carries
    only the SECOND-order sums, so this rides this group's own rolling power sums rather than that kernel.)
  * ``longest_streak_{W}m`` — the longest run of consecutive same-direction one-minute returns, normalized by
    the window. A run length is a sequential state machine over the ordered returns (not a sum), so it is the
    one piece evaluated by a light per-window pass over the return signs.

``compute_latest`` runs the SAME ``compute()`` on the buffer SLICED to this group's trailing window
(``LOOKBACK_MINUTES`` = deepest declared window + slack) before filtering to T — not a second formulation, so
backfill and live stay identical by construction (guarded by tests/test_fp_latest.py); it just avoids re-rolling
the ~300m buffer the group never reads. This is not a ``ReductionGroup``, so the incremental reduction engine
does not run it.
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

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
RUN_TOL = 1e-4

# Live ``compute_latest`` slices the buffer to this trailing depth before running the SAME ``compute()``.
# The deepest declared window is ``max(WINDOWS)``; +15m of slack covers the 1-bar lag the per-minute return
# (``close.shift(1)``) needs at the window's start and leaves wide margin (correctness > speed). Too-tight a
# slice fails the generic parity test (tests/test_fp_latest.py) loudly — the guard.
LOOKBACK_MINUTES = max(WINDOWS) + 15
MIN_POINTS = 4.0  # the old codebase required >=4 closes for a meaningful residual distribution
# Skewness = m3 / m2**1.5; m2 is the residual VARIANCE (price^2 units). An absolute ``m2 > 0`` guard is too
# loose: when the window's price path is near-perfectly linear the residual variance collapses to f32 noise
# (~1e-18 of the price level) yet stays positive, so the ratio explodes by ~8 orders of magnitude (observed
# live: residual_skew up to ±1.6e9 vs the declared ±20 range). Gate instead on a RELATIVE residual spread:
# the residual std must exceed REL_RESID_FLOOR of the window's mean price, i.e. m2 > (REL_RESID_FLOOR·my)^2.
# 1e-6 is far below any real intraday tick noise, so it only nulls the genuinely-degenerate near-linear fits.
REL_RESID_FLOOR = 1e-6


def _residual_skew_column(w: int) -> pl.Expr:
    """Skewness of the OLS residuals over the trailing ``w`` minutes, from rolling power sums up to third order
    of the centered time axis (__x) and close. Residuals r = yc - slope·xc (yc/xc centered, origin-invariant);
    the residual central moments are
        Σr^2 = Syy_c - slope·Sxy_c
        Σr^3 = Syyy_c - 3·slope·Sxyy_c + 3·slope^2·Sxxy_c - slope^3·Sxxx_c
    with the centered third-order sums expanded from the raw rolling sums. Null where undefined (n<MIN_POINTS,
    zero x-variance, or a degenerate residual spread below REL_RESID_FLOOR of the price level — see the note
    on REL_RESID_FLOOR; the skew ratio m3/m2**1.5 blows up when m2 is f32 noise on a near-linear path)."""
    size = f"{w}m"

    def roll(name: str) -> pl.Expr:
        return pl.col(name).rolling_sum_by("minute", window_size=size).over("symbol")

    n = roll("__one")
    sx = roll("__x")
    sy = pl.col("close").rolling_sum_by("minute", window_size=size).over("symbol")
    sxx, sxy, syy = roll("__xx"), roll("__xy"), roll("__yy")
    sxxx, sxxy, sxyy, syyy = roll("__xxx"), roll("__xxy"), roll("__xyy"), roll("__yyy")
    mx = sx / n
    my = sy / n
    # centered sums (origin-invariant)
    sxx_c = sxx - sx * mx
    sxy_c = sxy - sx * my
    syy_c = syy - sy * my
    sxxx_c = sxxx - 3.0 * mx * sxx + 3.0 * mx * mx * sx - n * mx**3
    sxxy_c = sxxy - 2.0 * mx * sxy - my * sxx + 2.0 * mx * my * sx + mx * mx * sy - n * mx * mx * my
    sxyy_c = sxyy - 2.0 * my * sxy - mx * syy + 2.0 * mx * my * sy + my * my * sx - n * mx * my * my
    syyy_c = syyy - 3.0 * my * syy + 3.0 * my * my * sy - n * my**3
    slope = sxy_c / sxx_c
    ssr = syy_c - slope * sxy_c
    sr3 = syyy_c - 3.0 * slope * sxyy_c + 3.0 * slope * slope * sxxy_c - slope**3 * sxxx_c
    m2 = ssr / n
    m3 = sr3 / n
    resid_var_floor = (REL_RESID_FLOOR * my).pow(2)  # (rel_eps · price)^2 — degenerate near-linear cutoff
    defined = (n >= MIN_POINTS) & (sxx_c > 0.0) & (m2 > resid_var_floor)
    return pl.when(defined).then(m3 / m2.clip(lower_bound=0.0).pow(1.5)).otherwise(None)


def _global_run_length(present: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol GLOBAL run length of consecutive same-direction nonzero returns, on the present-return rows
    (``present`` is the frame filtered to rows with a non-null ``__ret``, symbol/minute-sorted). ``__rl`` at a
    bar = how many bars the same-sign run has reached at that bar (0 on a zero return — a flat bar breaks the
    run, matching the old codebase). A windowed longest run is then ``max_i min(__rl_i, position_in_window_i)``
    (capping a run so it cannot count bars before the window's start)."""
    sgn = pl.when(pl.col("__ret") > 0.0).then(1).when(pl.col("__ret") < 0.0).then(-1).otherwise(0)
    present = present.with_columns(sgn.alias("__sgn"))
    new_run = ((pl.col("__sgn") != pl.col("__sgn").shift(1).over("symbol")) | (pl.col("__sgn") == 0)).fill_null(True)
    present = present.with_columns(new_run.cum_sum().over("symbol").alias("__runid"))
    rank = pl.col("__sgn").cum_count().over(["symbol", "__runid"])
    run_length = pl.when(pl.col("__sgn") == 0).then(0).otherwise(rank - rank.min().over(["symbol", "__runid"]) + 1)
    return present.with_columns(run_length.cast(pl.Int64).alias("__rl"))


@register
class MomentumRunGroup(FeatureGroup):
    name = "momentum_run"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.TREND_QUALITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"residual_skew_{w}m", description=f"Skewness of the OLS trend residuals over {w} minutes: positive = price spends more time below the line then snaps up; asymmetry of the deviations.",
                            dtype="Float64", valid_range=(-20.0, 20.0), nan_policy="warmup", layer="A", tolerance=RUN_TOL)
            )
            specs.append(
                FeatureSpec(name=f"longest_streak_{w}m", description=f"Longest run of consecutive same-direction one-minute returns over {w} minutes, normalized by the window; high = a sustained one-way push.",
                            dtype="Float64", valid_range=(0.0, 1.5), nan_policy="warmup", layer="A", tolerance=RUN_TOL)
            )
        return specs

    def compute(self, ctx: BatchContext) -> pl.DataFrame:
        frame = ctx.frame("minute_agg").select(["symbol", "minute", "close"]).sort(["symbol", "minute"])
        if frame.height == 0:
            schema = {"symbol": pl.String, "minute": pl.Datetime("us", "UTC"), **{name: pl.Float64 for name in self.feature_names}}
            return pl.DataFrame(schema=schema)
        epoch = pl.col("minute").dt.epoch("s").cast(pl.Float64)
        # Center the time axis on each symbol's LATEST minute (not the frame's global earliest). Residual
        # moments are origin-invariant, so this is mathematically identical, but it bounds |__x| to the window
        # depth (<= max window) instead of the whole buffer (~250m). The skew rides THIRD-order centered sums
        # (sxxx_c, sxxy_c, ...), each a catastrophic-cancellation difference whose float rounding is acutely
        # sensitive to |__x|: a buffer-relative origin makes that rounding depend on how DEEP the buffer is, so
        # the whole-buffer rolling and the window-sliced compute_latest would round a near-zero skew differently
        # (a parity break on near-symmetric residuals). Anchoring on the shared per-symbol latest minute keeps
        # |__x| identical for the latest window in both paths, so the window-sliced live form matches the
        # backfill cell-for-cell (tests/test_fp_latest.py) — and conditions the third-order sums better overall.
        frame = frame.with_columns(
            ((epoch - epoch.max().over("symbol")) / 60.0).alias("__x"),
            pl.lit(1.0).alias("__one"),
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0).alias("__ret"),
        )
        x, y = pl.col("__x"), pl.col("close")
        frame = frame.with_columns(
            (x * x).alias("__xx"), (x * y).alias("__xy"), (y * y).alias("__yy"),
            (x * x * x).alias("__xxx"), (x * x * y).alias("__xxy"), (x * y * y).alias("__xyy"), (y * y * y).alias("__yyy"),
        )
        # residual_skew: vectorized over all windows in one with_columns.
        skew_feats = [_residual_skew_column(w).alias(f"residual_skew_{w}m") for w in WINDOWS]
        frame = frame.with_columns(skew_feats)
        out = frame.select(["symbol", "minute", *[f"residual_skew_{w}m" for w in WINDOWS]])
        # longest_streak: vectorized over the present-return rows' global run length, capped per window so a
        # run cannot count bars before the window's start: max_i min(__rl_i, position_in_window_i).
        present = self._present_run_lengths(frame)
        for w in WINDOWS:
            collected = present.rolling(index_column="minute", period=f"{w}m", group_by="symbol").agg(
                pl.col("__rl").alias("_rl")
            )
            capped = pl.col("_rl").list.eval(pl.min_horizontal(pl.element(), pl.element().cum_count())).list.max()
            n_ret = pl.col("_rl").list.len()
            piece = collected.select(
                "symbol",
                "minute",
                pl.when(n_ret >= 2).then(capped / float(w)).otherwise(None).cast(pl.Float64).alias(f"longest_streak_{w}m"),
            )
            out = out.join(piece, on=["symbol", "minute"], how="left")
        return out.select(["symbol", "minute", *self.feature_names])

    def compute_latest(self, ctx: BatchContext) -> pl.DataFrame:
        """Window-sliced live path: the SAME rolling ``compute()`` on the trailing ``LOOKBACK_MINUTES`` it reads,
        filtered to T. Parity-true by construction — the run-length is capped to in-window positions and the
        rolling power sums read only the trailing window, so bars dropped before the slice cannot change T."""
        return self.compute_latest_on_window(ctx, LOOKBACK_MINUTES)

    def _present_run_lengths(self, frame: pl.DataFrame) -> pl.DataFrame:
        """The present-return rows (non-null ``__ret``) with the per-symbol global run-length column ``__rl``."""
        present = frame.filter(pl.col("__ret").is_not_null()).select(["symbol", "minute", "__ret"])
        if present.height == 0:
            return present.with_columns(pl.lit(0, dtype=pl.Int64).alias("__rl"))
        return _global_run_length(present)
