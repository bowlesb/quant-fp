"""Per-symbol centering anchor for the reduction-stability std/OLS columns.

The ONE source of the centering anchor a reduction group's std power-sum (``Σ(v−a)²``) and OLS/corr
denominator center on, so the ``Σx²−(Σx)²/n`` cancellation on LARGE-magnitude raw values (share volume ~1e6)
stays conditioned — the batch-vs-canonical FORMULA gap that gated volume / price_volume / market_beta /
residual_analysis (docs/CENTERED_STD_DESIGN.md, PROVEN: centering drops the std rel-err 3.1e-6 → 1.1e-16).

THE PARITY-CRITICAL CONTRACT: the anchor is a per-symbol CONSTANT read IDENTICALLY by the backfill batch
path AND the live/incremental path — derived from the same per-session-fixed ``daily`` snapshot the
daily-broadcast groups (daily_beta) already read, rounded to 2 SIGNIFICANT FIGURES so it tracks each symbol's
magnitude closely (``|v−a|/v`` <= ~0.05 — enough to condition the squared sums) without being data-noise-
sensitive (a measured global anchor is CATASTROPHIC on small-volume symbols, so it MUST be per-symbol-scale;
a nearest-power-of-ten anchor was too coarse). Because both paths center on the SAME
per-symbol constant and the variance/OLS are SHIFT-INVARIANT, the centered result is VALUE-IDENTICAL to the
raw form in exact arithmetic — only the float conditioning changes (so trust is preserved, fp unchanged).

The anchor is attached as a column on the minute frame BEFORE either path consumes it (so the incremental
engine, which folds the frame as-is and does not call ``prepare``, sees the identical anchor the batch
marshal does). A symbol absent from the daily snapshot gets anchor 0.0 (no centering — its volume is then
small/new, where the raw form is already well-conditioned)."""

from __future__ import annotations

import polars as pl

# The anchor column name attached to the minute frame (one per centered source column).
ANCHOR_PREFIX = "__anchor_"


def anchor_column(source_col: str) -> str:
    """The minute-frame column carrying the per-symbol centering anchor for ``source_col``."""
    return f"{ANCHOR_PREFIX}{source_col}"


_ANCHOR_SIG_FIGS = 2  # round the anchor to 2 significant figures: |v−a|/v <= ~0.05 (measured: centered std
# rel-err ~1e-16 vs the raw 3e-6 breach, across 5e5..5e7), while staying a STABLE per-symbol constant
# (insensitive to day-to-day volume noise, so the anchor does not flap between sessions). A nearest-power-of-
# ten anchor was too coarse (up to 5x off -> the cancellation persisted, measured); 2 sig figs is the sweet
# spot — close enough to condition the squared sums, coarse enough to be reproducible.


def sigfig_rounded_anchor(value: pl.Expr) -> pl.Expr:
    """A magnitude-tracking, reproducible per-symbol anchor: ``value`` rounded to ``_ANCHOR_SIG_FIGS``
    significant figures (so ``|value − anchor| / value`` is tiny, conditioning the centered power sums, yet
    the anchor is a stable constant insensitive to small inter-session volume noise). 0.0 for a non-positive
    / null value (no centering — those rows are small/absent, where the raw form is already well-conditioned).
    Computed as ``round(value / 10**e, ...) * 10**e`` with ``e = floor(log10(value)) − (sig − 1)`` — pure
    polars so it evaluates identically in the batch marshal and the incremental fold."""
    safe = pl.when(value > 0.0).then(value).otherwise(None)
    exponent = safe.log10().floor() - (float(_ANCHOR_SIG_FIGS) - 1.0)
    scale = pl.lit(10.0).pow(exponent)
    rounded = (safe / scale).round(0) * scale
    return pl.when(safe.is_not_null()).then(rounded).otherwise(0.0)


def attach_volume_anchor(frame: pl.DataFrame, daily: pl.DataFrame) -> pl.DataFrame:
    """Attach the per-symbol volume centering anchor to ``frame`` (keyed on symbol), from the ``daily``
    snapshot's most-recent per-symbol volume (log10-rounded). The SAME join both the backfill batch path and
    the live seed path apply, so the anchor column is identical in both — the parity-critical invariant.

    ``daily`` is the per-session-fixed snapshot (symbol, date, volume); the anchor uses each symbol's LATEST
    daily volume (the prior completed day in production), rounded to 2 sig figs. A symbol not in ``daily``
    gets 0.0.
    """
    latest = (
        daily.select(["symbol", "date", "volume"])
        .sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("volume").last().alias("_vol"))
        .with_columns(sigfig_rounded_anchor(pl.col("_vol")).alias(anchor_column("volume")))
        .select(["symbol", anchor_column("volume")])
    )
    return frame.join(latest, on="symbol", how="left").with_columns(
        pl.col(anchor_column("volume")).fill_null(0.0)
    )
