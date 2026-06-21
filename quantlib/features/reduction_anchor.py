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

# The reduction centers PER-MINUTE volume, but the ``daily`` snapshot carries each symbol's daily-BAR total
# (~one trading session's worth of minutes larger). Divide the daily total by the session-minute count to
# land the anchor at the per-minute scale the centering must track; otherwise |v−a|/v is ~SESSION_MINUTES
# (a ~2-order mismatch) and the centering only partially conditions the squared sums (the residual volume
# breach the real-data FP_INCREMENTAL soak surfaced: ~0.4% of minutes, worst ratio 209x). RTH is 6.5h = 390
# minutes; captured days run longer (extended hours, 440-950 min by name), so /390 slightly OVER-estimates
# per-minute volume for extended-hours-heavy names — but 2-sig-fig rounding absorbs that: a real-data A/B
# soak measured 0/779 breaches (worst 0.0) at /390, /600 AND /780, vs 4/779 at /1. /390 is the principled,
# well-known constant and sits safely inside the breach-free band.
SESSION_MINUTES = 390.0


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
    daily volume (the prior completed day in production), DIVIDED by the session-minute count so it lands at
    the per-minute scale the centering tracks (see ``SESSION_MINUTES``), then rounded to 2 sig figs. A symbol
    not in ``daily`` gets 0.0.
    """
    latest = (
        daily.select(["symbol", "date", "volume"])
        .sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("volume").last().alias("_vol"))
        .with_columns(
            sigfig_rounded_anchor(pl.col("_vol") / SESSION_MINUTES).alias(anchor_column("volume"))
        )
        .select(["symbol", anchor_column("volume")])
    )
    return frame.join(latest, on="symbol", how="left").with_columns(
        pl.col(anchor_column("volume")).fill_null(0.0)
    )


def attach_reduction_anchors(frames: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    """Attach every centered-std reduction anchor onto ``frames["minute_agg"]`` in place of the original
    frame, from the per-session snapshots ``frames`` already holds — the SINGLE wiring point shared by
    production capture (``capture.py``) and backfill (``materialize.py``) so the anchor column is present on
    the minute frame BEFORE the reduction engine seeds/folds it (the incremental fold reads it as-is) AND
    before ``runnable`` is evaluated (volume's ``InputSpec`` declares the anchor column, so the group is only
    selected once the column exists). Value-additive: the centered variance is shift-invariant, so this only
    changes float conditioning, never a feature value (fp unchanged).

    Currently the volume anchor, sourced from the ``daily`` snapshot via ``attach_volume_anchor``. A no-op
    (returns ``frames`` unchanged) when ``minute_agg`` is absent, ``daily`` is absent (no anchor source —
    the centered groups then stay unrunnable rather than centering on a fabricated anchor), or the anchor
    column is ALREADY present (idempotent — a frame that came pre-anchored from a test harness or a prior
    call is not re-joined)."""
    minute_agg = frames.get("minute_agg")
    daily = frames.get("daily")
    if minute_agg is None or daily is None:
        return frames
    if anchor_column("volume") in minute_agg.columns:
        return frames
    return {**frames, "minute_agg": attach_volume_anchor(minute_agg, daily)}
