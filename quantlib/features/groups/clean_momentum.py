"""Clean-momentum composite: one tradeability score per window (family: TREND_QUALITY, Layer A).

A single number for "is this a clean, tradeable trend?" combining the pieces the sibling groups compute, all
from this group's OWN reductions so it stays parity-true on every path (it recomputes the slope, R^2, and
residual std from the same OLS power sums + reductions, rather than reading another group's output frame — the
platform composes by re-deriving, never by cross-group joins):

  * normalized slope (fractional price move per minute, like ``trend_quality.price_slope``),
  * R^2 of the close-vs-time fit (how straight the move is),
  * residual std as a percent of price (how tightly price hugs the line — the ``residual_analysis`` closed form
    ``sqrt(std_close^2·(n-1)·(1-R^2)/n) / mean_close · 100``).

``clean_momentum_score_{W}m`` is the old codebase's 0–1 blend (slope magnitude capped, plus R^2, plus a
low-residual bonus); ``momentum_quality_flag_{W}m`` is its binary "high-quality setup" gate (significant slope
AND R^2 > 0.7 AND low residuals). Both are pure functions of the three reductions above.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import (
    ReductionGroup,
    StatefulRegressor,
    mean_,
    r2_,
    slope_,
    std_,
    sum_,
)
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
CLEAN_TOL = 1e-4

# Old-codebase blend weights / caps (slope here is a FRACTIONAL move per minute; the old code used percent,
# so 0.1%/min -> 0.001 and 0.02%/min -> 0.0002 in fractional terms; residual_std stays in percent units).
SLOPE_CAP = 0.001  # fractional slope at which the slope component saturates (was 0.1 %/min)
SLOPE_WEIGHT = 0.4
R2_WEIGHT = 0.3
RESID_WEIGHT = 0.3
RESID_SCALE = 0.5  # residual_std (%) at which the low-residual bonus reaches zero
FLAG_SLOPE_MIN = 0.0002  # |slope| threshold for the quality flag (was 0.02 %/min)
FLAG_R2_MIN = 0.7
FLAG_RESID_MAX = 0.3  # residual_std (%) ceiling for the quality flag


def _norm_slope(w: int) -> pl.Expr:
    return slope_("cm_clean", w) / mean_("cm_close", w)  # fractional price move per minute


def _resid_std_pct(w: int) -> pl.Expr:
    n = sum_("cm_one", w)
    var_resid = std_("cm_close", w) ** 2 * (n - 1.0) * (1.0 - r2_("cm_clean", w)) / n
    return (var_resid.clip(lower_bound=0.0).sqrt() / mean_("cm_close", w)) * 100.0


@register
class CleanMomentumScoreGroup(ReductionGroup):
    name = "clean_momentum"
    # 1.1.0: inherits trend_quality's n==2 r2 guard (r2 at the b==2 corner is now exactly 1.0).
    version = "1.1.0"
    owner = "modeller"
    type = FeatureType.TREND_QUALITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close")),)
    # The score blends the OLS R² (r2_("cm_clean")) of close on time, so it inherited trend_quality's
    # near-perfect-fit conditioning. Closed AT SOURCE by the time-OLS origin-rebase (PR #132, well-conditioned
    # n>=3) plus the n==2 perfect-fit guard (_OLS_PERFECT_FIT_COUNT) emitting r2=1.0 exactly at the b==2 corner
    # — batch==incremental cell-for-cell on smooth/degenerate/n==2 walks. The guard changes the degenerate r2
    # value (0.9998->1.0), which flows into the score/flag at those cells -> the version bump above.
    # NO-GO for FP_INCREMENTAL (real-data soak, scripts/incremental_realdata_soak.py, 2026-06-17): breaches the
    # incremental==batch parity self-check on ~1.5% of minutes (worst ~620x) — a power-sum cancellation
    # degenerate cell the synthetic stream never reproduces. Same class as the parked corr-denom groups; stays
    # on the batch path until the cancellation-free reduction fix lands.
    incremental_safe = False

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"clean_momentum_score_{w}m", description=f"Composite 0-1 clean-momentum score over {w} minutes: blends slope magnitude, R-squared, and low residuals — high = a steep, straight, tight trend.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A", tolerance=CLEAN_TOL)
            )
            specs.append(
                FeatureSpec(name=f"momentum_quality_flag_{w}m", description=f"1.0 when the {w}-minute trend is a high-quality setup (significant slope AND R-squared over 0.7 AND low residuals), else 0.0.",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="warmup", layer="A", tolerance=CLEAN_TOL, storage="UInt8")
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        # Namespaced (cm_) so the canonical columns never collide with another group's close-mean/std in the
        # unified single-pass emit (e.g. trend_quality's __mean_close_<w>).
        return {
            "cm_close": (pl.col("close"), ("mean", "std"), WINDOWS),
            "cm_one": (pl.lit(1.0), ("sum",), WINDOWS),
        }

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        epoch = pl.col("minute").dt.epoch("s").cast(pl.Float64)
        centered_t = (epoch - epoch.min()) / 60.0
        return {"cm_clean": (centered_t, pl.col("close"), ("slope", "r2"), WINDOWS)}

    def stateful_regressors(self) -> dict[str, list[StatefulRegressor]]:
        return {"cm_clean": [StatefulRegressor(slot="x", kind="time")]}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            abs_slope = _norm_slope(w).abs()
            r2 = r2_("cm_clean", w)
            resid_std = _resid_std_pct(w)
            slope_score = (abs_slope / SLOPE_CAP).clip(upper_bound=1.0) * SLOPE_WEIGHT
            r2_score = r2 * R2_WEIGHT
            resid_score = (1.0 - resid_std / RESID_SCALE).clip(lower_bound=0.0) * RESID_WEIGHT
            score = slope_score + r2_score + resid_score
            feats[f"clean_momentum_score_{w}m"] = pl.when(resid_std.is_null()).then(None).otherwise(score)
            flag = (abs_slope > FLAG_SLOPE_MIN) & (r2 > FLAG_R2_MIN) & (resid_std < FLAG_RESID_MAX)
            feats[f"momentum_quality_flag_{w}m"] = (
                pl.when(resid_std.is_null()).then(None).otherwise(flag.cast(pl.Float64))
            )
        return feats
