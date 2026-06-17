"""Stream symbol-day CLEANLINESS — isolate real parity bugs from capture-contamination.

THE PROBLEM this solves. A windowed/breadth feature (e.g. ``volume_zscore_30m``, a 30-minute rolling
stat) is computed correctly by both ``compute_latest`` (live) and ``compute`` (backfill), yet on a day
where live capture RESTARTED mid-session the live stream is missing a block of minutes. Backfill reads
the complete tape, so for the minutes AFTER the gap the two windows see different inputs and the feature
legitimately diverges — not because the feature logic is wrong, but because the live DATA was lost.
Grading that day as a parity FAILURE would condemn a correct feature. So we must first decide, per
(symbol, day), whether the live capture for that symbol-day is CLEAN enough to be a fair parity test.

SESSION SCOPE — EXTENDED HOURS ARE NOT A CONTAMINATION SIGNAL. We capture pre-market (~08:00 UTC / 04:00
ET), the regular session (13:30–20:00 UTC / 09:30–16:00 ET), and post-market (to ~24:00 UTC). A full
liquid-name day is ~850+ minutes, NOT 390 — and extended-hours minutes are legitimately SPARSE: an
illiquid name may print few or zero pre/post-market bars, and a minute with no trade has no bar (normal,
not a gap). Requiring full-day contiguous coverage would wrongly flag almost every symbol-day. So the
cleanliness check is scoped to the REGULAR SESSION ONLY (``rth_mask``), which IS dense for any actively
traded name; extended-hours coverage is bonus, never a contamination signal.

THE HEURISTIC (documented in docs/PARITY_LIFECYCLE.md). Within the regular session, a capture restart is
what we must catch: the live stream loses an INTERNAL BLOCK of minutes that backfill (the complete tape)
had, so post-gap windows legitimately diverge. We measure that block directly:

  1. NO INTERNAL MISSING RUN (the primary signal): the longest contiguous run of regular-session minutes
     that BACKFILL produced but the STREAM did NOT must be <= ``MAX_GAP_MINUTES``. A restart leaves a
     multi-minute hole > this; a single missed print does not. A thin name that prints few bars passes
     trivially — it has no internal hole RELATIVE TO BACKFILL, the only fair reference.
  2. COVERAGE FLOOR (a permissive secondary signal): distinct regular-session stream minutes >=
     ``MIN_COVERAGE_FRAC`` of the minutes backfill produced — catches a stream that is sparse EVERYWHERE
     vs a dense backfill (e.g. capture started late and never caught up) without one long internal run.
     The denominator is backfill-present minutes, never a flat 390, so a thin name is not penalised.

A symbol-day failing EITHER test is CONTAMINATED: its per-cell comparisons are recorded but EXCLUDED from
the trust grade. A feature is only condemned by failures on CLEAN symbol-days — exactly the
``compute_latest != compute`` bug we want to catch.

Pure polars over the joined verdict long-frame; no I/O, no wall-clock — unit-testable in isolation.
"""

from __future__ import annotations

import polars as pl

from quantlib.features.session import et_minute_of_day, rth_mask

SESSION_MINUTES = 390  # 09:30–16:00 ET distinct REGULAR-session minutes (extended hours are NOT counted)
MIN_COVERAGE_FRAC = 0.90  # permissive floor: >= 90% of backfill-present regular-session minutes present live
MAX_GAP_MINUTES = 5  # longest tolerated internal run of backfill-had-but-stream-missing minutes (restart > this)
# A symbol-day must have at least this many backfill-present regular-session minutes to be a FAIR parity
# test. A name that printed only a few dozen minutes (a thin/illiquid ticker, a preferred share, a warrant)
# trivially passes the no-internal-gap + coverage checks — it has no internal hole because it barely traded
# — yet its windowed features are DEGENERATE (near-zero denominators, flat near-zero values) so a tiny
# absolute difference becomes a huge RELATIVE error and masquerades as a parity failure. Grading off such
# names manufactures false DIVERGENT verdicts (root-caused 2026-06-15: a capture-restart day where the only
# symbols surviving the gap check were thin names with ~47 backfill minutes -> 383 spurious defects). A
# liquid name runs ~390; this floor admits a partial-but-substantial session while excluding the degenerate
# thin tail. It is a SECONDARY gate: a symbol below it is "contaminated" for grading purposes (reason
# thin_session), so it contributes no clean comparison rather than a false failure.
MIN_BACKFILL_MINUTES = 120  # >= ~1/3 of a regular session of printed minutes to be a fair parity test

# GATHER-COHERENCE (the cross-sectional contamination dimension the per-symbol check above cannot see).
# A market-wide breadth scalar (breadth_up_5m, ...) is broadcast IDENTICALLY to every symbol's row at a
# given minute: in a clean single-gather capture there is EXACTLY ONE distinct value per minute. When the
# universe-wide gather FRAGMENTS — a capture restart / SIP-contention day spins up several concurrent gather
# processes, each reducing over a PARTIAL universe — each writes its own partial breadth scalar, so that
# minute carries >1 distinct value. This is invisible to symbol_day_cleanliness (every symbol still HAS a
# row with a non-null ret_1m, just a wrong cold-ring / partial-universe value), so a fragmented day passes
# the per-symbol coverage+gap checks yet mis-grades EVERY cross-sectional and deep-window feature. We detect
# it directly: the fraction of regular-session minutes whose broadcast scalar is incoherent (>1 distinct
# value). Above MAX_INCOHERENT_FRAC the day's gather is fragmented and the day is NOT a fair parity test for
# the universe-reduce / deep-window cohort. (2026-06-15: 322/364 RTH minutes carried up to 8 distinct
# breadth values — the ~8 concurrent partial-universe gathers the restart spawned.)
MAX_INCOHERENT_FRAC = 0.05  # > this fraction of RTH minutes with a multi-valued broadcast scalar = fragmented


def gather_coherence(stream_broadcast: pl.DataFrame, value_col: str) -> dict[str, float | int | bool]:
    """Day-level GATHER-COHERENCE verdict from a stream frame of a broadcast cross-sectional scalar.

    ``stream_broadcast`` is the live (source=stream) rows for ONE broadcast cross-sectional feature
    (e.g. breadth_up_5m) over the day — columns (symbol, minute, ``value_col``). The feature is the SAME
    scalar for every symbol at a minute, so a CLEAN single-gather capture has exactly one distinct value per
    regular-session minute. We count the regular-session minutes carrying >1 distinct value (the
    partial-universe fragments a restart/contention day produces) and flag the day fragmented when their
    fraction exceeds ``MAX_INCOHERENT_FRAC``.

    Returns ``rth_minutes`` / ``incoherent_minutes`` / ``incoherent_frac`` / ``is_coherent`` (a single
    distinct value almost everywhere). An empty frame is vacuously coherent (no breadth captured -> nothing
    to certify here; the per-symbol checks still gate grading)."""
    if stream_broadcast.height == 0:
        return {"rth_minutes": 0, "incoherent_minutes": 0, "incoherent_frac": 0.0, "is_coherent": True}
    rth = stream_broadcast.filter(rth_mask(pl.col("minute")))
    if rth.height == 0:
        return {"rth_minutes": 0, "incoherent_minutes": 0, "incoherent_frac": 0.0, "is_coherent": True}
    per_minute = rth.group_by("minute").agg(pl.col(value_col).n_unique().alias("_distinct"))
    rth_minutes = per_minute.height
    incoherent_minutes = int(per_minute.filter(pl.col("_distinct") > 1).height)
    incoherent_frac = incoherent_minutes / rth_minutes
    return {
        "rth_minutes": rth_minutes,
        "incoherent_minutes": incoherent_minutes,
        "incoherent_frac": incoherent_frac,
        "is_coherent": incoherent_frac <= MAX_INCOHERENT_FRAC,
    }


def symbol_day_cleanliness(joined: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol CLEAN/contaminated decision for one day from a joined live+backfill verdict frame.

    ``joined`` must carry ``symbol``, ``minute``, a live column and its ``_bk`` backfill twin for at
    least one feature — we derive a per-row "live present" / "backfill present" signal structurally: a
    minute is present-live if ANY feature's live value is non-null there, present-backfill if ANY ``_bk``
    value is non-null. (We pass the already-RTH-masked, universe-pinned join from the sweep so the session
    scoping/membership are consistent with grading.)

    The contamination signal is measured RELATIVE TO BACKFILL within the regular session: the longest
    contiguous run of minutes backfill produced but the stream lacked (a capture restart), plus a
    permissive coverage floor. Extended-hours minutes are excluded entirely (``rth_mask``).

    Returns one row per symbol: (symbol, n_stream_minutes, n_backfill_minutes, coverage_frac,
    max_gap_minutes, is_clean, reason).
    """
    live_cols = [col for col in joined.columns if col not in ("symbol", "minute", "tier") and not col.endswith("_bk")]
    back_cols = [col for col in joined.columns if col.endswith("_bk")]
    if not live_cols or not back_cols:
        raise ValueError("cleanliness needs at least one live feature column and its _bk twin in the join")

    rth = joined.filter(rth_mask(pl.col("minute")))  # REGULAR SESSION ONLY — extended hours never counted
    live_present = pl.any_horizontal([pl.col(col).is_not_null() for col in live_cols])
    back_present = pl.any_horizontal([pl.col(col).is_not_null() for col in back_cols])
    marked = rth.with_columns(
        live_present.alias("_live_present"),
        back_present.alias("_back_present"),
        et_minute_of_day(pl.col("minute")).alias("_etm"),
    )
    # Backfill is the reference (the complete tape). A regular-session minute is a "miss" when backfill
    # produced it but the stream did not — what a capture restart leaves behind. We measure the LONGEST
    # CONTIGUOUS run of such misses over backfill's minutes (sparse extended-hours minutes are already
    # excluded; a thin name with few-but-fully-matched backfill minutes has no miss run, so it passes).
    backfill = marked.filter(pl.col("_back_present")).select("symbol", "_etm").unique()
    stream = marked.filter(pl.col("_live_present")).select("symbol", "_etm").unique()
    aligned = backfill.join(
        stream.with_columns(pl.lit(True).alias("_in_stream")), on=["symbol", "_etm"], how="left"
    ).with_columns(pl.col("_in_stream").fill_null(False))

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
    miss_runs = aligned.sort("symbol", "_etm").with_columns(
        # a new run starts whenever the miss-state flips; a cumulative sum per symbol labels each run.
        (pl.col("_in_stream") != pl.col("_in_stream").shift(1).over("symbol"))
        .cum_sum()
        .over("symbol")
        .alias("_run_id")
    )
    longest_miss = (
        miss_runs.filter(~pl.col("_in_stream"))
        .group_by("symbol", "_run_id")
        .agg(pl.len().alias("_run_len"))
        .group_by("symbol")
        .agg(pl.col("_run_len").max().alias("max_gap_minutes"))
    )
    result = per_symbol.join(longest_miss, on="symbol", how="left").with_columns(
        pl.col("max_gap_minutes").fill_null(0)  # no miss run at all -> contiguous w.r.t. backfill
    )
    coverage = (
        pl.when(pl.col("n_backfill_minutes") > 0)
        .then(pl.col("n_stream_minutes") / pl.col("n_backfill_minutes"))
        .otherwise(0.0)
    )
    result = result.with_columns(coverage.alias("coverage_frac"))
    is_clean = (
        (pl.col("coverage_frac") >= MIN_COVERAGE_FRAC)
        & (pl.col("max_gap_minutes") <= MAX_GAP_MINUTES)
        & (pl.col("n_backfill_minutes") >= MIN_BACKFILL_MINUTES)
    )
    reason = (
        pl.when(pl.col("max_gap_minutes") > MAX_GAP_MINUTES)
        .then(pl.lit("internal_gap"))
        .when(pl.col("coverage_frac") < MIN_COVERAGE_FRAC)
        .then(pl.lit("low_coverage"))
        .when(pl.col("n_backfill_minutes") < MIN_BACKFILL_MINUTES)
        .then(pl.lit("thin_session"))
        .otherwise(pl.lit("clean"))
    )
    return result.with_columns(is_clean.alias("is_clean"), reason.alias("reason")).sort("symbol")


def clean_symbols(cleanliness: pl.DataFrame) -> list[str]:
    """The symbols whose stream capture is clean enough to be a fair parity test for the day."""
    if cleanliness.height == 0:
        return []
    return cleanliness.filter(pl.col("is_clean"))["symbol"].to_list()
