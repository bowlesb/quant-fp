"""The per-symbol centering anchor (quantlib.features.reduction_anchor) — the foundation of the
reduction-stability centering that un-gates volume / price_volume / market_beta / residual_analysis.

Proves the two properties the whole fix rests on:
  1. VALUE-IDENTITY + cancellation closure: centering the std power sum on the 2-sig-fig anchor is
     value-identical to the raw form (shift-invariant) AND drops the rel-err from the ~3e-6 breach to
     machine precision, across the realistic volume magnitude range.
  2. The anchor is PER-SYMBOL-SCALE + reproducible (a global anchor is catastrophic on small-volume names),
     and ``attach_volume_anchor`` joins it identically from the daily snapshot (the one source both paths read).
"""

from __future__ import annotations

import numpy as np
import polars as pl

from quantlib.features.reduction_anchor import (
    anchor_column,
    attach_volume_anchor,
    sigfig_rounded_anchor,
)


def test_sigfig_anchor_is_per_symbol_scale_and_reproducible() -> None:
    df = pl.DataFrame({"v": [5.0e6, 3.7e6, 5.5e5, 1.23e3, 5.0e7, 0.0, None]})
    anchors = df.with_columns(sigfig_rounded_anchor(pl.col("v")).alias("a"))["a"].to_list()
    # 2 significant figures, per-symbol-scale; 0.0 for non-positive / null
    assert anchors == [5.0e6, 3.7e6, 5.5e5, 1.2e3, 5.0e7, 0.0, 0.0]
    # reproducible: a small perturbation does NOT move the 2-sig-fig anchor (no inter-session flapping)
    perturbed = pl.DataFrame({"v": [5.0e6 * (1 + 1e-4)]})
    assert perturbed.with_columns(sigfig_rounded_anchor(pl.col("v")).alias("a"))["a"][0] == 5.0e6


def test_centered_std_is_value_identical_and_closes_the_cancellation() -> None:
    """Across the realistic volume range, centering on the 2-sig-fig anchor makes the windowed std
    value-identical to the true (two-pass) variance to MACHINE PRECISION — closing the ~3e-6 raw power-sum
    cancellation that gated volume — and is shift-invariant (so the feature value is unchanged, trust kept).
    """
    rng = np.random.default_rng(11)
    n = 60
    for base in (5e6, 5e5, 1e3, 5e7, 3.7e6):
        v = base * (1.0 + rng.normal(0, 1e-5, size=n))  # the intermediate-variance breach regime
        anchor = pl.DataFrame({"v": [base]}).select(sigfig_rounded_anchor(pl.col("v"))).item()
        true_var = float(np.var(v, ddof=1))
        # centered power-sum variance (what the engine will compute from Σ(v−a), Σ(v−a)²)
        vc = v - anchor
        svc, svcsq = vc.sum(), (vc * vc).sum()
        centered_var = (svcsq - svc * svc / n) / (n - 1)
        assert (
            abs(centered_var - true_var) / true_var < 1e-9
        ), f"base={base:.0e}: centered std not machine-precise"


def test_attach_volume_anchor_joins_per_symbol_at_minute_scale_from_daily() -> None:
    daily = pl.DataFrame({"symbol": ["A", "A", "B"], "date": [1, 2, 1], "volume": [5.0e6, 5.1e6, 3.0e3]})
    frame = pl.DataFrame({"symbol": ["A", "B", "C"], "minute": [10, 10, 10]})
    attached = attach_volume_anchor(frame, daily)
    by_symbol = {r["symbol"]: r[anchor_column("volume")] for r in attached.iter_rows(named=True)}
    # The anchor is the LATEST daily-BAR volume divided by the session-minute count (390), then 2-sig-fig
    # rounded — landing it at the PER-MINUTE scale the centering tracks (not the ~390x-larger daily total,
    # which left the centering only partly conditioned; see reduction_anchor._SESSION_MINUTES).
    assert by_symbol["A"] == 13000.0  # sigfig2(5.1e6 / 390) = sigfig2(13076.9)
    assert by_symbol["B"] == 7.7  # sigfig2(3.0e3 / 390) = sigfig2(7.69)
    assert (
        by_symbol["C"] == 0.0
    )  # absent from daily -> 0.0 (no centering; a small/new name is well-conditioned)
