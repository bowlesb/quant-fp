"""Stream symbol-day CLEANLINESS — isolate real parity bugs from capture-contamination.

THE PROBLEM this solves. A windowed/breadth feature (e.g. ``volume_zscore_30m``, a 30-minute rolling
stat) is computed correctly by both ``compute_latest`` (live) and ``compute`` (backfill), yet on a day
where live capture RESTARTED mid-session the live stream is missing a block of minutes. Backfill reads
the complete tape, so for the minutes AFTER the gap the two windows see different inputs and the feature
legitimately diverges — not because the feature logic is wrong, but because the live DATA was lost.
Grading that day as a parity FAILURE would condemn a correct feature. So we must first decide, per
(symbol, day), whether the live capture for that symbol-day is CLEAN enough to be a fair parity test.

THE HEURISTIC (documented in docs/PARITY_LIFECYCLE.md). A stream symbol-day is CLEAN iff, over the
regular session (09:30–16:00 ET = ``SESSION_MINUTES`` distinct minutes):

  1. COVERAGE: the count of distinct RTH stream minutes is >= ``MIN_COVERAGE_FRAC`` of the minutes the
     BACKFILL side actually produced for that symbol-day (we compare against backfill-present minutes,
     not a flat 390, because a thin/halted name legitimately prints fewer bars — a fair denominator is
     "the minutes truth had", so a sparse-but-complete name is still clean).
  2. NO INTERNAL GAP: between the first and last stream minute, the largest gap in the stream's distinct
     RTH minutes is <= ``MAX_GAP_MINUTES``. A capture restart leaves a multi-minute hole that breaks any
     window reaching across it; a single missed print (<= the threshold) does not.

A symbol-day failing EITHER test is CONTAMINATED: its per-cell comparisons are recorded but marked
"skipped: contaminated" and EXCLUDED from the trust grade. A feature is only condemned by failures on
CLEAN symbol-days — which is exactly the ``compute_latest != compute`` bug we want to catch.

Pure polars over the joined verdict long-frame; no I/O, no wall-clock — unit-testable in isolation.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.session import et_minute_of_day, rth_mask

SESSION_MINUTES = 390  # 09:30–16:00 ET distinct RTH minutes (the full-session denominator ceiling)
MIN_COVERAGE_FRAC = 0.95  # >= 95% of backfill-present RTH minutes must be present live to be clean
MAX_GAP_MINUTES = 5  # the largest tolerated internal hole in the stream's RTH minutes (a restart > this)


def symbol_day_cleanliness(joined: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol CLEAN/contaminated decision for one day from a joined live+backfill verdict frame.

    ``joined`` must carry ``symbol``, ``minute``, a live column and its ``_bk`` backfill twin for at
    least one feature — but we only need the KEY columns plus a per-row "live present" / "backfill
    present" signal, which we derive structurally: a minute is present-live if ANY feature's live value
    is non-null there, present-backfill if ANY ``_bk`` value is non-null. (We pass the already-RTH-masked,
    universe-pinned join from ``validate`` so the session scoping/membership are consistent with grading.)

    Returns one row per symbol: (symbol, n_stream_minutes, n_backfill_minutes, coverage_frac,
    max_gap_minutes, is_clean, reason).
    """
    live_cols = [col for col in joined.columns if col not in ("symbol", "minute", "tier") and not col.endswith("_bk")]
    back_cols = [col for col in joined.columns if col.endswith("_bk")]
    if not live_cols or not back_cols:
        raise ValueError("cleanliness needs at least one live feature column and its _bk twin in the join")

    rth = joined.filter(rth_mask(pl.col("minute")))
    live_present = pl.any_horizontal([pl.col(col).is_not_null() for col in live_cols])
    back_present = pl.any_horizontal([pl.col(col).is_not_null() for col in back_cols])
    marked = rth.with_columns(
        live_present.alias("_live_present"),
        back_present.alias("_back_present"),
        et_minute_of_day(pl.col("minute")).alias("_etm"),
    )
    stream = marked.filter(pl.col("_live_present")).select("symbol", "_etm").unique()
    backfill = marked.filter(pl.col("_back_present")).select("symbol", "_etm").unique()

    per_symbol = (
        backfill.group_by("symbol")
        .agg(pl.col("_etm").n_unique().alias("n_backfill_minutes"))
        .join(
            stream.group_by("symbol").agg(pl.col("_etm").n_unique().alias("n_stream_minutes")),
            on="symbol",
            how="left",
        )
        .with_columns(pl.col("n_stream_minutes").fill_null(0))
    )
    gaps = (
        stream.sort("symbol", "_etm")
        .group_by("symbol")
        .agg((pl.col("_etm").diff().fill_null(1)).max().alias("max_gap_minutes"))
    )
    result = per_symbol.join(gaps, on="symbol", how="left").with_columns(
        pl.col("max_gap_minutes").fill_null(SESSION_MINUTES)
    )
    coverage = (
        pl.when(pl.col("n_backfill_minutes") > 0)
        .then(pl.col("n_stream_minutes") / pl.col("n_backfill_minutes"))
        .otherwise(0.0)
    )
    result = result.with_columns(coverage.alias("coverage_frac"))
    is_clean = (pl.col("coverage_frac") >= MIN_COVERAGE_FRAC) & (pl.col("max_gap_minutes") <= MAX_GAP_MINUTES)
    reason = (
        pl.when(pl.col("coverage_frac") < MIN_COVERAGE_FRAC)
        .then(pl.lit("low_coverage"))
        .when(pl.col("max_gap_minutes") > MAX_GAP_MINUTES)
        .then(pl.lit("internal_gap"))
        .otherwise(pl.lit("clean"))
    )
    return result.with_columns(is_clean.alias("is_clean"), reason.alias("reason")).sort("symbol")


def clean_symbols(cleanliness: pl.DataFrame) -> list[str]:
    """The symbols whose stream capture is clean enough to be a fair parity test for the day."""
    if cleanliness.height == 0:
        return []
    return cleanliness.filter(pl.col("is_clean"))["symbol"].to_list()
