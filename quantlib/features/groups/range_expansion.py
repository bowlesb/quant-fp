"""Intrabar range-expansion ratio (family: VOLATILITY, Layer A).

WHY (feature-invention batch 3, experiments/2026-06-19-feature-invention): when a name's intrabar
high-low range is already expanding — the recent few minutes' range running hot vs its own trailing
hour — a 2%-vol-BURST is more imminent. In the batch-3 screen against the validated vol-burst label
(``|fwd close-to-close ret| >= 2%`` over 5/20/30m, the same target ``realized_range`` +
``large_print_burst`` were promoted on) ``f_range_expansion`` is a stable-sign burst predictor across
ALL THREE horizons (AUC-0.5 +0.08, z 5-15, sign +,+,+) and sits on the "acceleration" channel —
distinct from the "concentration" (``print_hhi``) and "size-entropy" channels of the same batch. It is
the cleanest economic story of the batch: range already expanding → onset of vol-of-vol → burst.

``realized_range`` already ships the trailing MEAN of ``(high-low)/close`` at fixed short windows; this
is the RATIO of the recent-window mean to the trailing-window mean — the SHAPE of how that range is
changing, not its level. ``range_expansion_{r}_{w}m`` = ``mean((high-low)/close over r minutes)`` /
``mean((high-low)/close over w minutes)`` (r < w). > 1 means range is expanding, < 1 contracting.

This is a clean ``ReductionGroup`` (a ratio of two bounded windowed means of the SAME per-bar
non-negative ratio) — parity-true by construction (``compute_latest`` == ``compute``; guarded by
tests/test_fp_latest.py). RT-GREEN (two windowed means → O(1) incremental, ~2.5ms floor tier) and
PARITY-GREEN. The per-bar value guards its ``close > 0`` divisor (Guard 2) so a degenerate zero-price
bar is NULL on both paths; the ratio guards its denominator (the trailing mean) > 0 (Guard 2, a plain
mean of non-negative terms — sign-robust, never a cancellation difference) and an ``is_finite()``
backstop converts any stray non-finite to the agreed NULL identically.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.base import (
    FeatureSpec,
    FeatureType,
    InputSpec,
)
from quantlib.features.declarative import ReductionGroup, mean_
from quantlib.features.registry import register

# (recent_window, trailing_window) pairs — recent range vs trailing range. The screen used 10m vs 60m;
# the 5m-vs-30m sibling gives a shorter-horizon read on the same expansion shape.
WINDOW_PAIRS: tuple[tuple[int, int], ...] = ((5, 30), (10, 60))
WINDOWS: tuple[int, ...] = tuple(sorted({w for pair in WINDOW_PAIRS for w in pair}))


@register
class RangeExpansionGroup(ReductionGroup):
    name = "range_expansion"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.VOLATILITY
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "high", "low", "close")),)
    # NO-GO for FP_INCREMENTAL (real-data soak, scripts/incremental_realdata_soak.py, 2026-06-17): breaches the
    # incremental==batch parity self-check on ~7.8% of minutes (null/non-null flip at range_expansion_5_30m) —
    # the trailing-mean RATIO denom `>0` guard straddles between the batch fresh-sum and the incremental
    # running-sum on a near-zero-range trailing window. The synthetic stream doesn't reproduce the real gappy
    # structure. Stays on the batch path until the consistently-guarded reduction-denom fix lands.
    incremental_safe = False

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"range_expansion_{recent}_{trailing}m",
                description=(
                    f"Ratio of the trailing {recent}-minute mean intrabar high-low range "
                    f"((high-low)/close) to the trailing {trailing}-minute mean — recent range relative to "
                    f"the longer trailing range. > 1 means range is expanding (vol-of-vol onset, a stable "
                    f"vol-burst precursor in the batch-3 screen, z 5-15), < 1 contracting. Null on warmup or "
                    f"a zero trailing-range window."
                ),
                dtype="Float64",
                valid_range=(0.0, None),
                nan_policy="warmup",
                layer="A",
            )
            for recent, trailing in WINDOW_PAIRS
        ]

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        # Per-bar realized range fraction; guard the close>0 divisor (Guard 2) so a degenerate zero-price
        # bar contributes NULL (excluded from both window means) identically on both paths, never inf/nan.
        rng = (
            pl.when(pl.col("close") > 0.0)
            .then((pl.col("high") - pl.col("low")) / pl.col("close"))
            .otherwise(None)
        )
        return {"rng": (rng, ("mean",), WINDOWS)}

    def assemble(self) -> dict[str, pl.Expr]:
        feats: dict[str, pl.Expr] = {}
        for recent, trailing in WINDOW_PAIRS:
            num = mean_("rng", recent)
            denom = mean_("rng", trailing)
            # Guard 2: the denominator is a plain mean of non-negative per-bar ranges (a sum of
            # non-negative terms / count, NOT a cancellation difference), so its sign is robust — a
            # `denom > 0` guard is sufficient and correct. A flat/zero-range trailing window -> NULL on
            # both paths. is_finite() backstop converts any stray non-finite to the agreed NULL.
            ratio = pl.when(denom > 0.0).then(num / denom).otherwise(pl.lit(None, dtype=pl.Float64))
            feats[f"range_expansion_{recent}_{trailing}m"] = (
                pl.when(ratio.is_finite()).then(ratio).otherwise(pl.lit(None, dtype=pl.Float64))
            )
        return feats
