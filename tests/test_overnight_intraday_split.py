"""Unit tests for overnight_intraday_split — the overnight/intraday return decomposition (W11 substrate).

Hand-built daily + minute_agg frames with known open/close/prev_close lock in the intraday return, the
overnight/intraday asymmetry, and the overnight-share + zero-move-null edge. Parity covered by test_fp_latest.
"""
from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

D0 = datetime(2026, 6, 11, tzinfo=timezone.utc)
D1 = datetime(2026, 6, 12, tzinfo=timezone.utc)


def _ctx(open0: float, close0: float, open1: float, close1: float) -> BatchContext:
    daily = pl.DataFrame({
        "symbol": ["AAA", "AAA"],
        "date": [D0.date(), D1.date()],
        "open": [open0, open1],
        "close": [close0, close1],
    })
    # minute_agg: a couple of minutes on day D1 (the daily features broadcast to them).
    minute = pl.DataFrame({
        "symbol": ["AAA", "AAA"],
        "minute": [D1.replace(hour=14, minute=0), D1.replace(hour=14, minute=1)],
    })
    return BatchContext(frames={"daily": daily, "minute_agg": minute})


def _run(ctx: BatchContext) -> dict:
    out = run_group(REGISTRY.get_group("overnight_intraday_split"), ctx)
    return out.row(0, named=True)


def test_intraday_return() -> None:
    # day1: open 100 -> close 103 => intraday = 0.03
    row = _run(_ctx(90.0, 100.0, 100.0, 103.0))
    assert row["intraday_ret"] == pytest.approx(0.03)


def test_overnight_minus_intraday_asymmetry() -> None:
    # prev_close 100, open 102 (overnight +0.02), close 102 (intraday 0) -> asymmetry = 0.02 - 0 = 0.02
    row = _run(_ctx(95.0, 100.0, 102.0, 102.0))
    assert row["overnight_minus_intraday"] == pytest.approx(0.02)
    # a name that gaps up overnight then fully reverses intraday: open 102, close 100 (intraday ~ -0.0196)
    row2 = _run(_ctx(95.0, 100.0, 102.0, 100.0))
    assert row2["overnight_minus_intraday"] == pytest.approx(0.02 - (100.0 / 102.0 - 1.0))


def test_overnight_share() -> None:
    # overnight +0.02, intraday +0.02 (roughly) -> share ~ 0.5
    row = _run(_ctx(95.0, 100.0, 102.0, 102.0 * 1.02))
    assert 0.45 < row["overnight_share"] < 0.55


def test_overnight_share_null_on_zero_move() -> None:
    # open == prev_close AND close == open -> no move -> share undefined -> null
    row = _run(_ctx(95.0, 100.0, 100.0, 100.0))
    assert row["overnight_share"] is None


def test_warmup_null_first_day() -> None:
    # the first day has no prev_close -> overnight (and the asymmetry/share) are null on D0; intraday is fine.
    # Build a single-day panel where the minute is on D0 (no prior day) -> overnight-dependent cells null.
    daily = pl.DataFrame({"symbol": ["AAA"], "date": [D0.date()], "open": [100.0], "close": [101.0]})
    minute = pl.DataFrame({"symbol": ["AAA"], "minute": [D0.replace(hour=14)]})
    out = run_group(REGISTRY.get_group("overnight_intraday_split"), BatchContext(frames={"daily": daily, "minute_agg": minute}))
    row = out.row(0, named=True)
    assert row["intraday_ret"] == pytest.approx(0.01)        # close/open works with no prior day
    assert row["overnight_minus_intraday"] is None           # needs prev_close


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
