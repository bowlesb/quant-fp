"""Volume-leads-price lagged cross-correlation (family: CROSS_SECTIONAL, Layer A).

Does a surge in volume PRECEDE a price move? At each as-of minute T this regresses, over a trailing
window ENDING at T, the contemporaneous one-minute return on volume from ``k`` minutes earlier:

    corr( volume[t - k] , return[t] )   over all t in (T-w, T]

A positive correlation means elevated volume k minutes ago tended to be followed by a positive return —
volume leading price. The per-lag correlations expose the whole lead-lag profile so a model can read which
lag carries signal directly (an argmax "optimal lag" summary was intentionally dropped: argmax over the
near-equal correlations is discontinuous and flips on the float noise the additive-window kernel and the
rolling path legitimately differ by — i.e. it cannot be held parity-true, whereas each corr can).

POINT-IN-TIME / NO LOOK-AHEAD (the parity crux the original Edgar feature got wrong):
the "forward return" is reframed as a LAG on the volume side, NOT a forward shift on the return side. The
regressor ``volume.shift(k)`` (k > 0) reads only PAST bars, and the target ``return[t]`` uses bars <= t <= T.
The latest pair the window can include is ``(volume[T-k], return[T])`` — nothing after T is ever read. The
original group's ``vol_predicts_up`` / ``vol_predicts_move`` metrics, which averaged ACTUAL future returns
past the window edge, are deliberately NOT ported (they are look-ahead and cannot be made parity-safe).

This is a ``ReductionGroup`` riding the shared windowed-OLS kernel (same as ``market_beta``): the lagged
correlation is the ``corr`` of the windowed OLS of ``return`` (y) on ``volume.shift(k)`` (x), both per-symbol
short-lag columns. The engine generates BOTH the rolling backfill ``compute()`` and the single-pass live
``compute_latest()`` from one declaration, so live == backfill by construction and the generic parity test
(tests/test_fp_latest.py) guards cell-equality. Rows where the lagged volume or the return is null (warmup)
are excluded from the fit (``_ols_derived`` counts only paired rows), so an early window never biases the
correlation.
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import FeatureSpec, FeatureType, InputSpec
from quantlib.features.declarative import ReductionGroup, corr_
from quantlib.features.registry import register

WINDOWS: tuple[int, ...] = (15, 30, 60)
LAGS: tuple[int, ...] = (1, 2, 3, 5)
CORR_TOL = 1e-4


def _ret() -> pl.Expr:
    return pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0


@register
class VolumeLeadsPriceGroup(ReductionGroup):
    name = "volume_leads_price"
    version = "1.0.0"
    owner = "modeller"
    type = FeatureType.CROSS_SECTIONAL
    inputs = (InputSpec(name="minute_agg", columns=("symbol", "minute", "close", "volume")),)
    # Every feature is the corr of the windowed OLS of return (y) on LAGGED share volume (x). On a gappy window
    # the lagged-volume regressor x≈0, so the corr denominator denom_x = b·Σx²−(Σx)² is a difference of
    # float-noise. UNGATED by P2 (#283): the Neumaier compensated running sum (``_comp`` carries the add/expire
    # rounding loss) makes the corr-denom power sums match the batch fresh sum, so the straddle no longer
    # breaches — engine-vs-batch is CLEAN (0/295 across adversarial gappy/large-magnitude seeds; guarded by
    # test_gappy_denom_group_now_clean_after_p2_neumaier). NOT the `volume` std-FORMULA class (still gated).
    # Incremental_safe so it rides the running sums when FP_INCREMENTAL is enabled.
    incremental_safe = True

    def declare(self) -> list[FeatureSpec]:
        specs: list[FeatureSpec] = []
        for w in WINDOWS:
            for k in LAGS:
                specs.append(
                    FeatureSpec(
                        name=f"vol_leads_corr_lag{k}_{w}m",
                        description=(
                            f"Correlation over the trailing {w} minutes between volume {k} minute(s) earlier "
                            f"and the contemporaneous one-minute return — volume leading price at lag {k}, in [-1, 1]."
                        ),
                        dtype="Float64",
                        valid_range=(-1.01, 1.01),
                        nan_policy="sparse",
                        layer="A",
                        tolerance=CORR_TOL,
                    )
                )
        return specs

    def reduced(self) -> dict[str, tuple[pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        # No plain windowed reductions — every feature is a windowed OLS correlation (see regressions()).
        return {}

    def regressions(self) -> dict[str, tuple[pl.Expr, pl.Expr, tuple[str, ...], tuple[int, ...]]]:
        # One windowed OLS per lag: x = volume k minutes ago (past-only), y = this minute's return.
        return {
            f"vlp{k}": (pl.col("volume").shift(k).over("symbol"), _ret(), ("corr",), WINDOWS)
            for k in LAGS
        }

    def assemble(self) -> dict[str, pl.Expr]:
        return {
            f"vol_leads_corr_lag{k}_{w}m": corr_(f"vlp{k}", w) for w in WINDOWS for k in LAGS
        }
