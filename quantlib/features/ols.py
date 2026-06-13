"""Reusable windowed ordinary-least-squares kernel (time-anchored, per symbol).

Rolling slope / correlation / R-squared of y on x over a wall-clock window, expressed as a single
vectorized pass via rolling sums. Shared by any group that needs a regression: trend quality (close
on time), price-volume coupling (volume on return), market beta (stock return on market return),
OBV slope (on-balance-volume on time).

Two correctness rules baked in:
- **Pairing under nulls.** Only minutes where BOTH x and y are present contribute; partner-null rows
  are zeroed out of the sums and excluded from the count, so a warmup/missing value never biases the
  fit (matches the no-silent-degradation rule — a null is dropped from the pair, not treated as 0).
- **Conditioning.** OLS is invariant to the x-origin, so for a TIME regressor center it first with
  ``centered_minutes`` to keep magnitudes small; otherwise n*Sxx-Sx^2 cancels catastrophically and
  the live buffer (one origin) and backfill (another) would disagree past tolerance.
"""
from __future__ import annotations

import polars as pl


def centered_minutes(frame: pl.DataFrame, out: str = "_t", by: str = "minute") -> pl.DataFrame:
    """Add ``out`` = minutes since the frame's earliest ``by`` timestamp (a small, well-conditioned
    time regressor). Frame-relative origin is fine: OLS slope/corr/R^2 are origin-invariant, so live
    and backfill agree to floating point despite different earliest minutes."""
    t0 = frame.select(pl.col(by).dt.epoch("s").min()).item()
    return frame.with_columns(((pl.col(by).dt.epoch("s").cast(pl.Float64) - float(t0)) / 60.0).alias(out))


def ols_window_exprs(x: str, y: str, window: str, by: str = "minute", over: str = "symbol") -> dict[str, pl.Expr]:
    """Polars expressions for the rolling OLS of ``y`` on ``x`` over a time-anchored ``window`` (e.g.
    "30m"), grouped ``over`` symbol. Returns ``{"slope","corr","r2","n"}``. The frame must contain
    columns ``x``, ``y`` and ``by``, sorted by (``over``, ``by``). Undefined cells (fewer than two
    paired points, or zero x-variance) are null."""
    cx, cy = pl.col(x), pl.col(y)
    both = cx.is_not_null() & cy.is_not_null()
    x_paired = pl.when(both).then(cx).otherwise(0.0)
    y_paired = pl.when(both).then(cy).otherwise(0.0)
    n = both.cast(pl.Float64).rolling_sum_by(by, window).over(over)
    sx = x_paired.rolling_sum_by(by, window).over(over)
    sy = y_paired.rolling_sum_by(by, window).over(over)
    sxy = (x_paired * y_paired).rolling_sum_by(by, window).over(over)
    sxx = (x_paired * x_paired).rolling_sum_by(by, window).over(over)
    syy = (y_paired * y_paired).rolling_sum_by(by, window).over(over)
    denom_x = n * sxx - sx * sx
    denom_y = n * syy - sy * sy
    cov_n = n * sxy - sx * sy
    defined = (n >= 2.0) & (denom_x > 0.0)
    defined_corr = defined & (denom_y > 0.0)
    slope = pl.when(defined).then(cov_n / denom_x).otherwise(None).cast(pl.Float64)
    corr = pl.when(defined_corr).then(cov_n / (denom_x * denom_y).sqrt()).otherwise(None).cast(pl.Float64)
    r2 = pl.when(defined_corr).then((cov_n * cov_n) / (denom_x * denom_y)).otherwise(None).cast(pl.Float64)
    return {"slope": slope, "corr": corr, "r2": r2, "n": n}
