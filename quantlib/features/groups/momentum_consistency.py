"""Momentum-consistency features: whether a move is steadily one-directional or choppy (family: MOMENTUM, Layer A).

A clean trend's one-minute returns mostly point the same way as the net move and rarely flip; a choppy move
flips direction constantly even when it nets out a trend. These measure that path consistency from the
one-minute return signs over the trailing window, all as ADDITIVE windowed sums (slice-safe, so backfill /
live-batch / incremental agree by construction — the per-row contributions are short-lag functions of the
last few closes, never the whole buffer):

  * ``consistent_direction_{W}m`` — fraction of the window's one-minute returns whose sign matches the net
    W-direction (up-count / n when net up, down-count / n when net down, 0.5 when flat). The net direction is
    read at assemble from the at-T close and the close W minutes ago; the up/down counts are windowed sums of
    short-lag sign indicators.
  * ``reversal_count_{W}m`` — number of sign flips between consecutive nonzero returns, normalized by W (a
    windowed sum of a short-lag "flip" indicator over the last three closes).
  * ``longest_streak_{W}m`` — the longest run of same-direction returns is a sequential run statistic that is
    NOT additive; it lives in ``momentum_run`` (rolling path). It is intentionally absent here.
  * ``momentum_acceleration_{W}m`` — the "slope of slopes": mean one-minute return over the recent half-window
    minus the mean over the older half-window (×100). Both halves are windowed sums of the slice-safe
    one-minute return, so the second derivative folds like any reduction.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, pt_, sum_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
HALF: dict[int, int] = {w: w // 2 for w in WINDOWS}  # recent half-window for the acceleration second derivative
ACCEL_WINDOWS: tuple[int, ...] = tuple(sorted({*WINDOWS, *HALF.values()}))
CONSIST_TOL = 1e-6


@register
class MomentumConsistencyGroup(ReductionGroup):
    name = "momentum_consistency"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.MOMENTUM
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"consistent_direction_{w}m", description=f"Fraction of one-minute returns over {w} minutes whose sign matches the net move's direction (0.5 when the net move is flat); 1.0 is a steadily one-directional trend.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A", tolerance=CONSIST_TOL)
            )
            specs.append(
                FeatureSpec(name=f"reversal_count_{w}m", description=f"Number of direction reversals among consecutive one-minute returns over {w} minutes, normalized by the window length; high = choppy, indecisive.",
                            dtype="Float64", valid_range=(0.0, 1.5), nan_policy="warmup", layer="A", tolerance=CONSIST_TOL)
            )
            specs.append(
                FeatureSpec(name=f"momentum_acceleration_{w}m", description=f"Slope-of-slopes over {w} minutes: mean one-minute return in the recent half-window minus the older half-window (x100); positive = the move is speeding up.",
                            dtype="Float64", valid_range=(-50.0, 50.0), nan_policy="warmup", layer="A", tolerance=CONSIST_TOL)
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        close = pl.col("close")
        prev = close.shift(1).over("symbol")
        prev2 = close.shift(2).over("symbol")
        ret = close / prev - 1.0  # one-minute simple return (null when the immediate prior minute is absent)
        ret_prev = prev / prev2 - 1.0
        up = (ret > 0.0).cast(pl.Float64)
        down = (ret < 0.0).cast(pl.Float64)
        has_ret = ret.is_not_null().cast(pl.Float64)
        # A reversal: this return and the prior return are both nonzero and have opposite sign.
        flip = (
            ret.is_not_null() & ret_prev.is_not_null() & (ret != 0.0) & (ret_prev != 0.0) & ((ret > 0.0) != (ret_prev > 0.0))
        ).cast(pl.Float64)
        ret_filled = pl.when(ret.is_not_null()).then(ret).otherwise(0.0)
        # Reduced-column names are namespaced (mc_) so the canonical __<stat>_<name>_<w> columns never collide
        # with another group's reduced column of the same root name in the unified single-pass emit.
        return {
            "mc_up": (up, ("sum",), WINDOWS),
            "mc_down": (down, ("sum",), WINDOWS),
            "mc_nret": (has_ret, ("sum",), ACCEL_WINDOWS),
            "mc_flip": (flip, ("sum",), WINDOWS),
            "mc_ret": (ret_filled, ("sum",), ACCEL_WINDOWS),  # sum of one-minute returns (present-only)
        }

    def points(self) -> dict[str, pl.Expr]:
        pts: dict[str, pl.Expr] = {"c": pl.col("close")}
        for w in WINDOWS:
            pts[f"l{w}"] = pl.col("close").shift(w).over("symbol")
        return pts

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            n_ret = sum_("mc_nret", w)
            net = pt_("c") - pt_(f"l{w}")  # net move over the window (null until the W-ago bar exists)
            # consistent_direction: matched fraction by net direction; 0.5 when flat (old-codebase neutral).
            frac = (
                pl.when(net > 0.0)
                .then(sum_("mc_up", w) / n_ret)
                .when(net < 0.0)
                .then(sum_("mc_down", w) / n_ret)
                .otherwise(0.5)
            )
            feats[f"consistent_direction_{w}m"] = pl.when((n_ret > 0.0) & net.is_not_null()).then(frac).otherwise(None)
            feats[f"reversal_count_{w}m"] = pl.when(n_ret > 0.0).then(sum_("mc_flip", w) / float(w)).otherwise(None)
            half = HALF[w]
            recent_n = sum_("mc_nret", half)
            recent_sum = sum_("mc_ret", half)
            older_n = n_ret - recent_n
            older_sum = sum_("mc_ret", w) - recent_sum
            recent_mean = pl.when(recent_n > 0.0).then(recent_sum / recent_n).otherwise(None)
            older_mean = pl.when(older_n > 0.0).then(older_sum / older_n).otherwise(None)
            feats[f"momentum_acceleration_{w}m"] = (recent_mean - older_mean) * 100.0
        return feats
