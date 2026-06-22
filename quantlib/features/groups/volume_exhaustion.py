"""Volume-exhaustion / dry-up features from per-minute OHLCV (family: VOLUME, Layer A).

Participation-side and participation-trend measures that distinguish a move running OUT of fuel from one
still being fed:

- ``vol_down_up_ratio_{w}m`` — share volume printed on down bars (close < open) divided by that printed on
  up bars over the window. > 1 means selling participation dominates, < 1 means buying does; a classic
  exhaustion read (heavy down-volume that then dries up).
- ``vol_dryup_{w}m`` — the latest minute's volume divided by the window's mean per-minute volume. < 1 is a
  quieting tape (the current bar is below its recent norm — "dry-up"); > 1 is an expanding one.
- ``vol_contraction_{w}m`` — mean per-minute volume over a short trailing window divided by the mean over a
  longer trailing baseline (5m/30m, 10m/60m). < 1 is contracting participation (recent activity below its
  longer baseline — decelerating), > 1 expanding.

All three are pure windowed reductions over the EXISTING ``minute_agg`` columns (``open``, ``close``,
``volume``) — a ``ReductionGroup``, so the engine generates BOTH the rolling backfill ``compute()`` and the
single-pass ``compute_latest()`` from one declaration and they are parity-true by construction
(``tests/test_fp_latest.py`` guards cell-equality). Up/down classification uses only the SAME bar's
open/close (no shift, no future bar), so every cell reads data <= T — look-ahead-safe. A window with no
volume on the up side (up-volume below a tiny relative floor of total participating volume), or a
zero-volume window, yields null (the ratio is undefined) rather than a fabricated 0 — see ``_UP_VOL_REL_EPS``.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec, FeatureType, InputSpec
from quantlib.features.declarative import ReductionGroup, mean_, pt_, sum_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (5, 15, 30, 60)
# vol_contraction = (short trailing mean volume) / (longer trailing baseline mean volume): (short, long).
CONTRACTIONS: tuple[tuple[int, int], ...] = ((5, 30), (10, 60))

# Relative floor on the up-volume denominator of ``vol_down_up_ratio`` (mirrors the trade_freq_z /
# _OLS_DENOM_*_REL_EPS discipline). A bare ``vex_up_vol_sum > 0`` is the SIGN-at-threshold trap: when a window
# has NO up-bars the batch fresh-sum is EXACTLY 0 (→ null) but the incremental running sum can carry a tiny
# residual (~1e-15, e.g. when a co-resident time-OLS group's rebase realizes the shared Neumaier compensation),
# so ``> 0`` passes on one path and not the other → a null/non-null parity FLIP (ratio = down/1e-15 ≈ 1e15
# where batch nulls). Gating the up-volume on a fraction of the window's TOTAL participating volume
# (up + down) makes a genuinely-no-up-bar window null on BOTH paths (0 ≤ eps·down) while never touching a
# window with real up-volume (up ≫ eps·(up+down)). 1e-9 sits far above the running-sum residual and far below
# any real participation share. Value-identical to the bare guard on well-conditioned cells.
_UP_VOL_REL_EPS = 1e-9


@register
class VolumeExhaustionGroup(ReductionGroup):
    name = "volume_exhaustion"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLUME
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "open", "close", "volume")),)

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            specs.append(
                FeatureSpec(
                    name=f"vol_down_up_ratio_{w}m",
                    description=(
                        f"Share volume on down bars (close < open) divided by share volume on up bars over "
                        f"the trailing {w} minutes — selling-vs-buying participation; null if no up-bar volume."
                    ),
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="warmup",
                    layer="A",
                )
            )
            specs.append(
                FeatureSpec(
                    name=f"vol_dryup_{w}m",
                    description=(
                        f"Latest minute's volume divided by the mean per-minute volume over the trailing {w} "
                        f"minutes — below 1 is a quieting tape (dry-up), above 1 is expanding; null if mean is 0."
                    ),
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        for short, long in CONTRACTIONS:
            specs.append(
                FeatureSpec(
                    name=f"vol_contraction_{short}_{long}m",
                    description=(
                        f"Mean per-minute volume over the trailing {short} minutes divided by the mean over the "
                        f"trailing {long} minutes — below 1 is contracting participation; null if the {long}m mean is 0."
                    ),
                    dtype="Float64",
                    valid_range=(0.0, None),
                    nan_policy="warmup",
                    layer="A",
                )
            )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        is_down = pl.col("close") < pl.col("open")
        is_up = pl.col("close") > pl.col("open")
        down_vol = pl.when(is_down).then(pl.col("volume")).otherwise(0.0)
        up_vol = pl.when(is_up).then(pl.col("volume")).otherwise(0.0)
        # Mean volume is needed over the dry-up windows and over both legs of each contraction ratio.
        contraction_windows = {w for pair in CONTRACTIONS for w in pair}
        vol_windows = tuple(sorted(set(WINDOWS) | contraction_windows))
        return {
            "vex_down_vol": (down_vol, ("sum",), WINDOWS),
            "vex_up_vol": (up_vol, ("sum",), WINDOWS),
            "vex_vol": (pl.col("volume"), ("mean",), vol_windows),
        }

    def points(self) -> dict[str, pl.Expr]:
        return {"vol_t": pl.col("volume")}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for w in WINDOWS:
            feats[f"vol_down_up_ratio_{w}m"] = (
                pl.when(
                    sum_("vex_up_vol", w)
                    > _UP_VOL_REL_EPS * (sum_("vex_up_vol", w) + sum_("vex_down_vol", w))
                )
                .then(sum_("vex_down_vol", w) / sum_("vex_up_vol", w))
                .otherwise(None)
            )
            feats[f"vol_dryup_{w}m"] = (
                pl.when(mean_("vex_vol", w) > 0.0).then(pt_("vol_t") / mean_("vex_vol", w)).otherwise(None)
            )
        for short, long in CONTRACTIONS:
            feats[f"vol_contraction_{short}_{long}m"] = (
                pl.when(mean_("vex_vol", long) > 0.0)
                .then(mean_("vex_vol", short) / mean_("vex_vol", long))
                .otherwise(None)
            )
        return feats
