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

# Regular-session minutes per day. The ``daily`` snapshot carries each symbol's DAILY-BAR total volume, but
# the volume reduction centers PER-MINUTE volume (~390x smaller). Centering on the daily total leaves the
# anchor ~2 orders of magnitude ABOVE the per-minute value it conditions, so |v−a|/v is ~390 and the
# cancellation only PARTIALLY closes (measured: volume still breaches the incremental parity self-check on
# ~0.4% of real minutes, worst ~13.7x). Dividing the daily total by this count puts the anchor on the
# per-minute scale the std actually centers -> |v−a|/v small -> the cancellation closes fully (measured:
# 0/779 breaches, worst 0.0). The anchor stays a reproducible per-symbol constant (same daily snapshot, same
# divisor, both paths), and the centered variance is shift-invariant, so the feature VALUE is unchanged (fp
# unchanged) — only the float conditioning.
_RTH_MINUTES_PER_DAY = 390


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
    snapshot's most-recent per-symbol volume. The SAME join both the backfill batch path and the live seed
    path apply, so the anchor column is identical in both — the parity-critical invariant.

    ``daily`` is the per-session-fixed snapshot (symbol, date, volume); the anchor uses each symbol's LATEST
    daily volume — the DAILY-BAR total — converted to the PER-MINUTE scale (``/ _RTH_MINUTES_PER_DAY``) the
    volume reduction actually centers, then rounded to 2 sig figs. Centering on the un-scaled daily total left
    the anchor ~390x above the per-minute value and only partially closed the cancellation; the per-minute
    scaling closes it fully. A symbol not in ``daily`` gets 0.0.
    """
    latest = (
        daily.select(["symbol", "date", "volume"])
        .sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("volume").last().alias("_vol"))
        .with_columns(
            sigfig_rounded_anchor(pl.col("_vol") / _RTH_MINUTES_PER_DAY).alias(anchor_column("volume"))
        )
        .select(["symbol", anchor_column("volume")])
    )
    return frame.join(latest, on="symbol", how="left").with_columns(
        pl.col(anchor_column("volume")).fill_null(0.0)
    )


def attach_close_anchor(frame: pl.DataFrame, daily: pl.DataFrame) -> pl.DataFrame:
    """Attach the per-symbol CLOSE centering anchor to ``frame`` (keyed on symbol), from the ``daily``
    snapshot's most-recent per-symbol close. The SAME join the backfill batch path and the live seed path
    apply, so the anchor column is identical in both — the parity-critical invariant the y-side OLS
    conditioning (``FP_RUST_REDUCE``) relies on.

    Used by the OLS R²/corr/resid-std reductions that regress ``y = close`` on a time axis (trend_quality,
    clean_momentum, residual_analysis). Centering ``y`` on a per-symbol-constant close anchor keeps
    ``denom_y = b·Σ(y−a)² − (Σ(y−a))²`` and ``cov_n = b·Σ(x·(y−a)) − Σx·Σ(y−a)`` from cancelling large
    near-equal sums on raw close prices (~$45–$500), so the batch fresh-sum path and the incremental
    running-sum path round a near-perfect-fit r²/corr identically. OLS is translation-invariant in y, so the
    centered result is VALUE-IDENTICAL to the raw form in exact arithmetic — only the float conditioning
    changes (fp unchanged). Unlike the volume anchor (per-MINUTE scale) the close anchor is the daily-bar
    close, which IS already the per-minute price scale, so no rescaling: round it straight to 2 sig figs.
    A symbol not in ``daily`` gets 0.0 (no centering — its raw close is then the only conditioning available).
    """
    latest = (
        daily.select(["symbol", "date", "close"])
        .sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("close").last().alias("_close"))
        .with_columns(sigfig_rounded_anchor(pl.col("_close")).alias(anchor_column("close")))
        .select(["symbol", anchor_column("close")])
    )
    return frame.join(latest, on="symbol", how="left").with_columns(
        pl.col(anchor_column("close")).fill_null(0.0)
    )


def attach_return_anchor(frame: pl.DataFrame, daily: pl.DataFrame) -> pl.DataFrame:
    """Attach the per-symbol one-minute-RETURN centering anchor to ``frame`` (keyed on symbol), from the
    ``daily`` snapshot's most-recent per-symbol open/close. The SAME join the backfill batch path and the live
    seed path apply, so the anchor column is identical in both — the parity-critical invariant the distribution
    group's skew/kurtosis conditioning relies on.

    Used by ``distribution`` to center the one-minute return ``r`` before forming its power sums ``Σ(r−a)^k``
    (k=1..4) → central moments → skew/excess-kurtosis. Subtracting a per-symbol constant near the typical
    per-minute return shrinks ``(r−a)`` so the higher-power sums stay conditioned on a near-constant-return
    window. Central moments are translation-invariant, so the centered result is VALUE-IDENTICAL to the raw
    form in exact arithmetic — only the float conditioning changes (fp unchanged). The anchor is the daily-bar
    return ``close/open − 1`` on the per-minute scale (``/ _RTH_MINUTES_PER_DAY``, the same /390 rescale the
    volume anchor applies), rounded to 2 significant figures so it is a stable per-symbol constant. A symbol
    not in ``daily`` — or one whose daily return is zero/undefined — gets 0.0 (no centering; its raw return is
    then near-zero and already well-conditioned)."""
    daily_return = pl.col("_close") / pl.col("_open") - 1.0
    per_minute = daily_return / _RTH_MINUTES_PER_DAY
    # sigfig_rounded_anchor is magnitude-only (0.0 for non-positive); preserve the sign so a symbol drifting
    # DOWN at a constant rate (negative per-minute return) centers on a negative anchor, not 0.0.
    signed_anchor = pl.when(per_minute < 0.0).then(-sigfig_rounded_anchor(-per_minute)).otherwise(
        sigfig_rounded_anchor(per_minute)
    )
    latest = (
        daily.select(["symbol", "date", "open", "close"])
        .sort(["symbol", "date"])
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("open").last().alias("_open"), pl.col("close").last().alias("_close"))
        .with_columns(signed_anchor.alias(anchor_column("return")))
        .select(["symbol", anchor_column("return")])
    )
    return frame.join(latest, on="symbol", how="left").with_columns(
        pl.col(anchor_column("return")).fill_null(0.0)
    )


def attach_reduction_anchors(frames: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    """Attach every centered-std reduction anchor onto ``frames["minute_agg"]`` in place of the original
    frame, from the per-session snapshots ``frames`` already holds — the SINGLE wiring point shared by
    production capture (``capture.py``) and backfill (``materialize.py``) so the anchor column is present on
    the minute frame BEFORE the reduction engine seeds/folds it (the incremental fold reads it as-is) AND
    before ``runnable`` is evaluated (volume's ``InputSpec`` declares the anchor column, so the group is only
    selected once the column exists). Value-additive: the centered variance is shift-invariant, so this only
    changes float conditioning, never a feature value (fp unchanged).

    The volume anchor is sourced from the ``daily`` snapshot via ``attach_volume_anchor``. When ``daily`` is
    ABSENT (e.g. the 24/7 crypto capture, which has no daily-bar snapshot), the anchor column is still
    attached but set to the 0.0 sentinel — UNCENTERED, i.e. the raw power-sum form — so volume stays
    RUNNABLE rather than silently dropping out for want of an anchor source. 0.0 is the module's existing
    "no centering" value (``attach_volume_anchor`` already assigns it to any symbol absent from ``daily``),
    and it is well-conditioned where the raw form already was (crypto volume is moderate-magnitude). A no-op
    (returns ``frames`` unchanged) when ``minute_agg`` is absent or the anchor column is ALREADY present
    (idempotent — a frame that came pre-anchored from a test harness or a prior call is not re-joined)."""
    minute_agg = frames.get("minute_agg")
    if minute_agg is None:
        return frames
    if anchor_column("volume") in minute_agg.columns:
        return frames
    daily = frames.get("daily")
    if daily is None:
        anchored = minute_agg.with_columns(
            pl.lit(0.0).alias(anchor_column("volume")),
            pl.lit(0.0).alias(anchor_column("close")),
            pl.lit(0.0).alias(anchor_column("return")),
        )
        return {**frames, "minute_agg": anchored}
    anchored = attach_volume_anchor(minute_agg, daily)
    # The close + return anchors are only sourced when the daily snapshot carries the columns they need (the
    # daily-bar close / open+close); a snapshot that predates them falls back to the 0.0 sentinel (uncentered).
    # Both paths read these identical columns, so the centering stays parity-true.
    if "close" in daily.columns:
        anchored = attach_close_anchor(anchored, daily)
    else:
        anchored = anchored.with_columns(pl.lit(0.0).alias(anchor_column("close")))
    if "open" in daily.columns and "close" in daily.columns:
        anchored = attach_return_anchor(anchored, daily)
    else:
        anchored = anchored.with_columns(pl.lit(0.0).alias(anchor_column("return")))
    return {**frames, "minute_agg": anchored}
