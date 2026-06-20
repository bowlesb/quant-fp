"""Shared session-cumulative aggregation for the CumulativeState-kind groups (runner_state / dumper_state /
gap_fill_state) — the P3.1 single-pass realization of the CumulativeState kind on the live path.

The three groups each, independently, derived the ET-session columns, filtered minute_agg to the current
session, and ran a per-(symbol) ``group_by`` aggregate in their own ``compute_latest`` (the latest-only form
from PR #266/#267/#269). That is THREE tz-conversions + THREE session filters + THREE group_by passes over
the same buffer every minute — the per-group Python frame-build the P3 floor (the ~213ms other_emit) is made
of. This module does it ONCE: one tz/session derivation + one ``group_by(symbol)`` producing the UNION of the
accumulators all three need (``high.max`` / ``low.min`` / ``(close·volume).sum`` / ``open.first`` /
``close.last``), memoized on the minute_agg snapshot so the 2nd and 3rd group in the same shard-minute read
the shared aggregate instead of rebuilding it.

VALUE-IDENTICAL by construction: the per-(symbol, current-session) ``max``/``min``/``sum``/``first``/``last``
this shares is the EXACT aggregate each group's ``_assemble_latest`` computed (same RTH+session filter, same
partition, same reduce) — only computed once and shared, never re-derived per group. So each group's emitted
features are byte-identical to before (guarded by test_fp_latest + the #267/#269 adversarial oracle), and the
backfill ``compute()`` path is untouched (this is a live-path share, not a value change).
"""
from __future__ import annotations

import polars as pl

from quantlib.features.base import SessionCache
from quantlib.features.session import OPEN_MINUTE, et_minute_of_day

# The union of source columns the three groups read (runner: high/close/volume/open; dumper: low/close/volume/
# open; gap_fill: open/close). minute_agg always carries all of them.
_SESSION_SOURCE_COLS = ("symbol", "minute", "open", "high", "low", "close", "volume")

_AGG_CACHE = SessionCache()


def session_cumulative_agg(ctx_frame: pl.DataFrame, latest: object) -> pl.DataFrame:
    """The per-(symbol) session-cumulative aggregate over T's OWN session, shared across the CumulativeState
    groups. Returns one row per symbol present at ``latest`` with the union of accumulators
    (``_run_high`` / ``_run_low`` / ``_run_dollar`` / ``_sess_open`` / ``close`` / ``sdate`` / ``minute``).
    Memoized on the (minute_agg snapshot id+shape, latest) witness so the three groups compute it once."""
    witness = (id(ctx_frame), ctx_frame.height, latest)
    return _AGG_CACHE.get(witness, lambda: _compute(ctx_frame, latest))


def _compute(ctx_frame: pl.DataFrame, latest: object) -> pl.DataFrame:
    frame = ctx_frame.select(list(_SESSION_SOURCE_COLS)).with_columns(
        pl.col("minute").dt.convert_time_zone("America/New_York").dt.date().alias("sdate"),
        et_minute_of_day(pl.col("minute")).alias("_etm"),
    )
    latest_sdate = pl.lit(latest).dt.convert_time_zone("America/New_York").dt.date()
    session = frame.filter(
        (pl.col("_etm") >= OPEN_MINUTE) & (pl.col("sdate") == latest_sdate) & (pl.col("minute") <= latest)
    ).sort(["symbol", "minute"])
    return session.group_by("symbol", maintain_order=True).agg(
        pl.col("high").max().alias("_run_high"),
        pl.col("low").min().alias("_run_low"),
        (pl.col("close") * pl.col("volume")).sum().alias("_run_dollar"),
        pl.col("open").first().alias("_sess_open"),
        pl.col("close").last().alias("close"),
        pl.col("sdate").first().alias("sdate"),
        pl.col("minute").last().alias("minute"),
    )
