"""Canonical US-equity session boundaries + the documented warmup contract for stream/backfill parity.

THE WARMUP PARITY CONTRACT (docs/SESSION_WARMUP.md). Streaming and backfill produce IDENTICAL windowed
features during regular trading hours if and ONLY if both anchor their lookback to the SAME fixed
pre-open time. A time-based window ``(T-w, T]`` evaluated at an RTH minute T sees exactly the minutes
captured/available since that anchor; if both paths share the anchor and the same tape, the inputs are
identical cell-for-cell, so the outputs are too — parity by construction, including across the
pre-market boundary (we do NOT discard pre-market; both sides incorporate it identically).

We fix the anchor at ``WARMUP_MINUTES_BEFORE_OPEN`` before the 09:30 ET open (08:00 ET). The contract
that makes parity hold:
  1. Live capture subscribes and begins buffering at the anchor (08:00 ET), every session.
  2. Backfill recompute seeds its rolling windows from the SAME anchor and reaches NO further back —
     so it can never use pre-anchor minutes the live buffer never had.
  3. Bets — and validation/grading — are confined to RTH (``rth_mask``). A feature only needs to be
     warm and comparable inside the session, which keeps the warmup region out of the trust grade.

Warmth vs parity are distinct: with a 90-minute anchor, windows <= 90m are FULLY warm at the open;
windows > 90m (e.g. 180/240m) are still "warming" early in RTH (their window reaches before the
anchor and is truncated to since-anchor on BOTH sides) — they remain parity-consistent, just partial,
until enough RTH elapses. See the doc for the breadth-over-depth rationale for accepting that.
"""
from __future__ import annotations

import polars as pl

OPEN_MINUTE = 570  # 09:30 ET, minutes since ET midnight
CLOSE_MINUTE = 960  # 16:00 ET
WARMUP_MINUTES_BEFORE_OPEN = 90  # 1.5h: live capture AND backfill both anchor lookback here (08:00 ET)
WARMUP_START_MINUTE = OPEN_MINUTE - WARMUP_MINUTES_BEFORE_OPEN  # 480 = 08:00 ET


def et_minute_of_day(minute: pl.Expr) -> pl.Expr:
    """Minutes since ET midnight for a UTC minute timestamp (DST-correct via tz conversion)."""
    et = minute.dt.convert_time_zone("America/New_York")
    # cast to Int32 before *60 — dt.hour() is Int8 and 10*60 overflows it (calendar.py:70).
    return et.dt.hour().cast(pl.Int32) * 60 + et.dt.minute().cast(pl.Int32)


def rth_mask(minute: pl.Expr) -> pl.Expr:
    """True for minutes inside the 09:30-16:00 ET regular session — the bet/validation window."""
    minute_of_day = et_minute_of_day(minute)
    return (minute_of_day >= OPEN_MINUTE) & (minute_of_day < CLOSE_MINUTE)
