"""Price-volume interaction features from per-minute bars (family: PRICE_VOLUME, Layer A).

How volume lines up with price: where the close sits versus a volume-weighted average, how much of
the window's volume printed on up- vs down-bars, a volume-weighted money-flow position, the rolling
return/volume correlation, and the slope of on-balance volume. The ratio metrics are time-anchored
rolling sums (stable, no centering); the correlation and OBV-slope use the shared windowed-OLS
kernel (OBV-slope regresses on a centered time axis, so it is origin-invariant and parity-true).
"""
from __future__ import annotations

import polars as pl

from quantlib.features import declarative
from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, StatefulRegressor, corr_, mean_, pt_, slope_, sum_
from quantlib.features.reduction_anchor import anchor_column
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (3, 5, 10, 15, 20, 30, 45, 60, 90, 120)

_ANCHOR_VOLUME = anchor_column("volume")  # per-symbol volume anchor pv_correlation's y-side OLS conditioning centers on


@register
class PriceVolumeGroup(ReductionGroup):
    name = "price_volume"
    # 1.2.0: n==2 perfect-fit guard makes pv_correlation exactly sign(cov) at the b==2 corner. The y-centering
    # of pv_correlation's volume regressand (regression_y_anchor, FP_RUST_REDUCE) that conditions its corr denom
    # is VALUE-IDENTICAL (OLS is translation-invariant in y — only the float conditioning changes), so it is
    # NOT a version/fingerprint bump (same precedent as the close-y anchored groups).
    version = "1.2.0"
    owner = "modeller"
    type = FeatureType.PRICE_VOLUME
    inputs = (
        InputSpec(
            name="minute_agg",
            columns=("symbol", "minute", "high", "low", "close", "volume", _ANCHOR_VOLUME),
        ),
    )

    def regression_y_anchor(self) -> dict[str, str]:
        """Center pv_correlation's RAW-share-volume regressand on the per-symbol ``__anchor_volume`` constant.
        pv_correlation is a Pearson corr (cov / √(var_x·var_y)) of one-minute RETURN (x, ~1e-4) against raw
        share VOLUME (y, ~1e6) — so the large-magnitude cancellation is on the Y side: the corr y-side denom
        ``denom_y = b·Σy² − (Σy)²`` is a difference of near-equal large sums whose low bits the incremental
        running-Σy² rounds differently from the batch fresh-sum (measured worst self-check tol-ratio ~670× the
        production breach threshold). Centering y on the same per-minute-scale volume anchor the ``volume``
        group's centered std already reads conditions ``denom_y`` on small centered volume, so both paths round
        it identically. This rides the EXISTING y-anchor mechanism (the close-y groups' FP_RUST_REDUCE
        conditioning) — no new engine/kernel path. OLS corr is translation-invariant in y → value-identical (fp
        unchanged); only the float conditioning changes. The OBV-slope regression (``obv``) regresses a
        cumulative on a centered TIME axis (well-conditioned) and is NOT centered — only ``pv`` is declared."""
        return {"pv": anchor_column("volume")}

    @property
    def incremental_safe(self) -> bool:  # type: ignore[override]
        """SAFE to ride the incremental running sums ONLY when ``FP_RUST_REDUCE`` is on — pv_correlation's corr
        denom cancels on BOTH sides on degenerate cells, and both are now conditioned:

          * Y-SIDE (raw share VOLUME ~1e6): ``regression_y_anchor`` centers the volume regressand on the
            per-symbol ``__anchor_volume`` constant under FP_RUST_REDUCE → ``denom_y = b·Σ(y−a)² − (Σ(y−a))²``
            rounds identically on the batch fresh-sum and incremental running-sum paths (#402; synthetic proof
            tests/test_fp_price_volume_comoment.py — worst self-check tol-ratio 670x→<1x).
          * X-SIDE (one-minute RETURN ~1e-4): a near-CONSTANT-but-nonzero return (a flat/illiquid name grinding
            one direction) makes ``denom_x = b·Σr² − (Σr)²`` cancel at the ~1e-12 relative level — the batch and
            incremental Σr² straddle the old ``(Σx)²`` guard floor → a null/non-null parity FLIP (measured worst
            tol-ratio up to +Inf on a near-constant-return stream). A return has NO stable per-symbol anchor and
            NO rolling origin to center on, so the fix is the translation-invariant variance guard
            ``_OLS_DENOM_X_CENTERED_REL_EPS`` (declarative.py) — reject ``denom_x ≤ 1e-9·(b·Σx²)`` so a
            constant-return window is NULL on BOTH paths; it ships unconditionally in the shared OLS stat (all
            three twins) and is value-identical on well-conditioned cells.

        With FP_RUST_REDUCE OFF the y-side is uncentered (raw-volume cancellation re-exposed), so the group stays
        PARKED on the batch fresh-sum recompute (byte-identical under FP_INCREMENTAL). The flag default-OFF keeps
        today's behavior; the prod flip is the Lead's FP_RUST_REDUCE relaunch (the y-anchor + this property arm
        together). Mirrors how ``_y_anchor_exprs`` reads the live flag, so tests toggling
        ``declarative._USE_RUST_REDUCE`` drive both states in lockstep."""
        return declarative._USE_RUST_REDUCE

    def declare(self) -> list[FeatureSpec]:
        specs = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(name=f"vwap_deviation_{w}m", description=f"Close relative to its trailing {w}-minute volume-weighted average price (close/vwap - 1).",
                            dtype="Float64", valid_range=(-1.0, 5.0), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"up_volume_ratio_{w}m", description=f"Fraction of the trailing {w}-minute share volume that printed on up-bars (positive one-minute return).",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"down_volume_ratio_{w}m", description=f"Fraction of the trailing {w}-minute share volume that printed on down-bars (negative one-minute return).",
                            dtype="Float64", valid_range=(-0.01, 1.01), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"volume_delta_{w}m", description=f"Net directional volume over {w} minutes: (up-bar volume - down-bar volume) / total volume, in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"buying_pressure_{w}m", description=f"Volume-weighted money-flow position over {w} minutes: mean of (2*close-high-low)/(high-low) weighted by volume, in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="sparse", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"pv_correlation_{w}m", description=f"Rolling correlation of one-minute return and share volume over {w} minutes (does volume accompany up or down moves), in [-1, 1].",
                            dtype="Float64", valid_range=(-1.01, 1.01), nan_policy="warmup", layer="A")
            )
            specs.append(
                FeatureSpec(name=f"obv_slope_{w}m", description=f"Slope of on-balance volume regressed on time over {w} minutes, normalized by mean window volume (accumulation/distribution drift).",
                            dtype="Float64", nan_policy="warmup", layer="A", tolerance=1e-4)
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        rng = pl.col("high") - pl.col("low")
        mfm = pl.when(rng > 0.0).then((2.0 * pl.col("close") - pl.col("high") - pl.col("low")) / rng).otherwise(0.0)
        vol = pl.col("volume")
        return {
            "vol": (vol, ("sum", "mean"), WINDOWS),  # sum feeds the 4 ratios; mean normalizes obv_slope
            "cv": (pl.col("close") * vol, ("sum",), WINDOWS),
            "mfv": (mfm * vol, ("sum",), WINDOWS),
            "up": (pl.when(ret > 0.0).then(vol).otherwise(0.0), ("sum",), WINDOWS),
            "dn": (pl.when(ret < 0.0).then(vol).otherwise(0.0), ("sum",), WINDOWS),
        }

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        signed = pl.when(ret > 0.0).then(pl.col("volume")).when(ret < 0.0).then(-pl.col("volume")).otherwise(0.0)
        obv = signed.cum_sum().over("symbol")
        epoch = pl.col("minute").dt.epoch("s").cast(pl.Float64)
        centered_t = (epoch - epoch.min()) / 60.0  # frame-relative time regressor (OLS is origin-invariant)
        return {
            "pv": (ret, pl.col("volume"), ("corr",), WINDOWS),  # return-vs-volume correlation
            "obv": (centered_t, obv, ("slope",), WINDOWS),  # on-balance-volume slope on time
        }

    def stateful_regressors(self) -> dict[str, list[StatefulRegressor]]:
        ret = pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0
        signed = pl.when(ret > 0.0).then(pl.col("volume")).when(ret < 0.0).then(-pl.col("volume")).otherwise(0.0)
        return {
            "obv": [
                StatefulRegressor(slot="x", kind="time"),
                StatefulRegressor(slot="y", kind="cumulative", increment=signed),
            ]
        }

    def points(self) -> dict[str, pl.Expr]:
        return {"cT": pl.col("close")}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            vol_w = sum_("vol", w)
            feats[f"vwap_deviation_{w}m"] = pt_("cT") / (sum_("cv", w) / vol_w) - 1.0
            feats[f"up_volume_ratio_{w}m"] = sum_("up", w) / vol_w
            feats[f"down_volume_ratio_{w}m"] = sum_("dn", w) / vol_w
            feats[f"volume_delta_{w}m"] = (sum_("up", w) - sum_("dn", w)) / vol_w
            feats[f"buying_pressure_{w}m"] = sum_("mfv", w) / vol_w
            feats[f"pv_correlation_{w}m"] = corr_("pv", w)
            feats[f"obv_slope_{w}m"] = slope_("obv", w) / mean_("vol", w)
        return feats
