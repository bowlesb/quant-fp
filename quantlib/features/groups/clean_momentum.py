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

from quantlib.features import declarative
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
from quantlib.features.reduction_anchor import anchor_column
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
CLEAN_TOL = 1e-4
_ANCHOR_CLOSE = anchor_column("close")  # per-symbol close anchor the y-side OLS conditioning centers on

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

# momentum_quality_flag's >/< compares snap their continuous inputs to this many decimals (under FP_RUST_REDUCE)
# so the ~1e-12 batch-vs-incremental running-sum divergence cannot straddle a hard threshold (the
# sign-at-threshold trap). 6 dp is ~1e6× coarser than that float noise yet far finer than any meaningful
# resolution of r2 (~0.7), |slope| (~2e-4), or resid_std% (~0.3) — so it changes the flag only on the
# float-noise-ambiguous boundary cells, never a well-separated one.
FLAG_SNAP_DP = 6


def _snap(value: pl.Expr) -> pl.Expr:
    """Round a flag comparison input to FLAG_SNAP_DP decimals when FP_RUST_REDUCE is on (so batch and
    incremental land on the SAME side of a threshold), else pass it through unchanged (byte-identical
    to today's raw compare when the flag is off)."""
    return value.round(FLAG_SNAP_DP) if declarative._USE_RUST_REDUCE else value


def _norm_slope(w: int) -> pl.Expr:
    return slope_("cm_clean", w) / mean_("cm_close", w)  # fractional price move per minute


def _resid_std_pct(w: int) -> pl.Expr:
    n = sum_("cm_one", w)
    var_resid = std_("cm_close", w) ** 2 * (n - 1.0) * (1.0 - r2_("cm_clean", w)) / n
    return (var_resid.clip(lower_bound=0.0).sqrt() / mean_("cm_close", w)) * 100.0


@register
class CleanMomentumScoreGroup(ReductionGroup):
    name = "clean_momentum"
    # 1.2.0: momentum_quality_flag's hard-threshold conjunction ((r2>0.7)&(|slope|>2e-4)&(resid_std<0.3)) snaps
    # its comparison inputs to FLAG_SNAP_DP decimals before the >/< compares so the ~1e-12 batch-vs-incremental
    # rounding noise can no longer straddle a threshold (the sign-at-threshold trap). This changes the flag only
    # on cells within FLAG_SNAP_DP of a threshold (float-noise-ambiguous boundary cells) -> version bump. The
    # continuous score is UNCHANGED. 1.1.0 added trend_quality's n==2 r2 guard (r2 at the b==2 corner = 1.0).
    version = "1.2.0"
    owner = "modeller"
    type = FeatureType.TREND_QUALITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", _ANCHOR_CLOSE)),)
    # The score blends the OLS R² (r2_("cm_clean")) of close on time, so it inherited trend_quality's
    # near-perfect-fit conditioning, now closed by the y-side close anchor under FP_RUST_REDUCE
    # (regression_y_anchor) + the n==2 perfect-fit guard. The CONTINUOUS clean_momentum_score is parity-safe
    # under the anchor (real-tape soak worst tol-ratio 0.02× — value-identical batch==incremental). The only
    # residual breach is momentum_quality_flag, a hard-thresholded BINARY: a cell whose r2/|slope|/resid_std
    # sits on a knife-edge has the batch fresh-sum and incremental running-sum round the ~13th sig-digit to
    # OPPOSITE sides of a threshold -> a 0<->1 flip (the sign-at-threshold trap, ~1 cell/23k on the real tape).
    # _snap() conditions those compares so the flip can't happen (validated 0 flips on the 2026-06-17
    # soak co-resident with price_volume). See incremental_safe below.

    @property
    def incremental_safe(self) -> bool:  # type: ignore[override]
        """SAFE to ride the incremental running sums ONLY when ``FP_RUST_REDUCE`` is on. Two conditions had to
        close together:

          * The CONTINUOUS r2/resid-std y-side cancellation (the 620× former NO-GO): ``regression_y_anchor`` +
            ``centered_std`` center ``y = close`` / the close power sums on the per-symbol ``__anchor_close``
            constant under FP_RUST_REDUCE, conditioning ``denom_y``/the residual variance on small centered close
            so both paths round them identically (OLS/std are translation-invariant → value-identical, fp
            unchanged). Real-tape soak: clean_momentum_score worst 0.02× (clean).
          * The BINARY momentum_quality_flag sign-at-threshold flip: ``_snap`` rounds the flag's
            comparison inputs to ``FLAG_SNAP_DP`` decimals under FP_RUST_REDUCE so the ~1e-12 batch-vs-incremental
            divergence can no longer straddle a hard threshold. Real-tape soak: 0 flips (vs 1 boundary flip at
            momentum_quality_flag_5m / BAC without the snap).

        With FP_RUST_REDUCE OFF the y-side is uncentered AND the flag compares are raw (byte-identical to today),
        so the group stays PARKED on the batch fresh-sum recompute. The prod flip is the Lead's FP_RUST_REDUCE
        relaunch; the flag snap rides the same flag so its boundary-cell value change deploys with the version
        bump above (coordinated, re-trust). Mirrors price_volume's FR-gated property."""
        return declarative._USE_RUST_REDUCE

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"clean_momentum_score_{w}m",
                    description=f"Composite 0-1 clean-momentum score over {w} minutes: blends slope magnitude, R-squared, and low residuals — high = a steep, straight, tight trend.",
                    dtype="Float64",
                    valid_range=(-0.01, 1.01),
                    nan_policy="warmup",
                    layer="A",
                    tolerance=CLEAN_TOL,
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"momentum_quality_flag_{w}m",
                    description=f"1.0 when the {w}-minute trend is a high-quality setup (significant slope AND R-squared over 0.7 AND low residuals), else 0.0.",
                    dtype="Float64",
                    valid_range=(-0.01, 1.01),
                    nan_policy="warmup",
                    layer="A",
                    tolerance=CLEAN_TOL,
                    storage="UInt8",
                )
            )
        return specs

    def centered_std(self) -> dict[str, str]:
        # cm_close's std is sqrt((Σc²−(Σc)²/n)/(n−1)) on raw close (~$45–$500) — the SAME large-magnitude
        # cancellation volume's std hit. Under FP_RUST_REDUCE route it through the per-symbol centered power
        # sums Σ(c−a)/Σ(c−a)² (shift-invariant == raw var, but conditioned), so the residual-std term
        # (std_close²·(1−r2)) the score reads matches batch and incremental cell-for-cell. Empty when the flag
        # is off (raw power-sum std, byte-identical to today). Both paths read the SAME anchor column.
        if not declarative._USE_RUST_REDUCE:
            return {}
        return {"cm_close": _ANCHOR_CLOSE}

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

    def regression_y_anchor(self) -> dict[str, str]:
        # Center y=close on the per-symbol close anchor under FP_RUST_REDUCE — conditions the r2/resid-std
        # y-side cancellation (the 620x score breach) value-identically (OLS translation-invariant in y).
        return {"cm_clean": _ANCHOR_CLOSE}

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
            # Snap the hard-threshold compares (under FP_RUST_REDUCE) so the ~1e-12 batch-vs-incremental
            # running-sum divergence can't straddle a threshold and flip the binary flag 0<->1 (sign-at-threshold).
            flag = (
                (_snap(abs_slope) > FLAG_SLOPE_MIN)
                & (_snap(r2) > FLAG_R2_MIN)
                & (_snap(resid_std) < FLAG_RESID_MAX)
            )
            feats[f"momentum_quality_flag_{w}m"] = (
                pl.when(resid_std.is_null()).then(None).otherwise(flag.cast(pl.Float64))
            )
        return feats
