"""Session boundaries + the warmup-anchor RTH mask (the stream/backfill pre-open parity contract)."""
from __future__ import annotations

import datetime as dt

import polars as pl

from quantlib.features.session import (
    CLOSE_MINUTE,
    OPEN_MINUTE,
    WARMUP_START_MINUTE,
    rth_mask,
)


def test_warmup_anchor_is_ninety_minutes_before_open() -> None:
    assert OPEN_MINUTE - WARMUP_START_MINUTE == 90
    assert WARMUP_START_MINUTE == 480  # 08:00 ET
    assert (OPEN_MINUTE, CLOSE_MINUTE) == (570, 960)  # 09:30 / 16:00 ET


def test_rth_mask_includes_session_excludes_pre_and_post() -> None:
    # ET times expressed as UTC (EDT = UTC-4 in June): 08:00 ET=12:00Z, 09:30 ET=13:30Z, 15:59 ET=19:59Z, 16:00 ET=20:00Z
    minutes = [
        dt.datetime(2026, 6, 12, 12, 0, tzinfo=dt.timezone.utc),  # 08:00 ET pre-market warmup -> excluded
        dt.datetime(2026, 6, 12, 13, 30, tzinfo=dt.timezone.utc),  # 09:30 ET open -> included
        dt.datetime(2026, 6, 12, 19, 59, tzinfo=dt.timezone.utc),  # 15:59 ET -> included
        dt.datetime(2026, 6, 12, 20, 0, tzinfo=dt.timezone.utc),  # 16:00 ET close -> excluded (half-open)
    ]
    got = pl.DataFrame({"minute": minutes}).select(rth_mask(pl.col("minute")).alias("rth"))["rth"].to_list()
    assert got == [False, True, True, False]
