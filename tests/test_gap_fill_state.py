"""Unit tests for gap_fill_state — the running point-in-time gap-fill fraction.

Hand-built minute_agg + daily frames with a known gap + intraday path lock in the fill fraction
and the extended flag. Parity (compute_latest == compute on the last minute) is covered by the
generic tests/test_fp_latest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

# 13:30 UTC = 09:30 ET (June, EDT = UTC-4) — the session open.
OPEN = datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc)


def _ctx(prev_close: float, sess_open: float, closes: list[float]) -> BatchContext:
    rows = []
    for i, close in enumerate(closes):
        rows.append(
            {
                "symbol": "GAP",
                "minute": OPEN + timedelta(minutes=i),
                "open": sess_open if i == 0 else close,
                "close": float(close),
            }
        )
    minute = pl.DataFrame(rows)
    daily = pl.DataFrame(
        {
            "symbol": ["GAP", "GAP"],
            "date": [OPEN.date() - timedelta(days=1), OPEN.date()],
            "close": [prev_close, closes[-1]],
        }
    )
    return BatchContext(frames={"minute_agg": minute, "daily": daily})


def _row(out: pl.DataFrame, minute_idx: int) -> dict:
    return out.filter(pl.col("minute") == OPEN + timedelta(minutes=minute_idx)).row(
        0, named=True
    )


def test_gap_up_fill_fraction() -> None:
    # prev_close=$10, gap UP to open $11 (denom = 10-11 = -1). close path 11, 10.5, 10, 11.5.
    # fill = (close - 11)/(10 - 11) = -(close-11) = 11 - close.
    ctx = _ctx(10.0, 11.0, [11.0, 10.5, 10.0, 11.5])
    out = run_group(REGISTRY.get_group("gap_fill_state"), ctx)
    assert _row(out, 0)["gap_fill_fraction"] == pytest.approx(
        0.0
    )  # at the open, no fill
    assert _row(out, 1)["gap_fill_fraction"] == pytest.approx(
        0.5
    )  # halfway back to prev_close
    assert _row(out, 2)["gap_fill_fraction"] == pytest.approx(
        1.0
    )  # fully filled to prev_close
    assert _row(out, 3)["gap_fill_fraction"] == pytest.approx(
        -0.5
    )  # extended past the open


def test_gap_extended_flag() -> None:
    ctx = _ctx(10.0, 11.0, [11.0, 10.5, 10.0, 11.5])
    out = run_group(REGISTRY.get_group("gap_fill_state"), ctx)
    assert _row(out, 1)["gap_extended"] == 0  # filling
    assert _row(out, 3)["gap_extended"] == 1  # extended (fraction < 0)


def test_gap_down_fill_fraction() -> None:
    # prev_close=$10, gap DOWN to open $9 (denom = 10-9 = +1). close 9 -> fill 0; close 9.5 -> 0.5; 10 -> 1.
    ctx = _ctx(10.0, 9.0, [9.0, 9.5, 10.0])
    out = run_group(REGISTRY.get_group("gap_fill_state"), ctx)
    assert _row(out, 0)["gap_fill_fraction"] == pytest.approx(0.0)
    assert _row(out, 1)["gap_fill_fraction"] == pytest.approx(0.5)
    assert _row(out, 2)["gap_fill_fraction"] == pytest.approx(1.0)


def test_zero_gap_is_null() -> None:
    # open == prev_close → no gap → null fill + null extended.
    ctx = _ctx(10.0, 10.0, [10.0, 10.5])
    out = run_group(REGISTRY.get_group("gap_fill_state"), ctx)
    assert _row(out, 1)["gap_fill_fraction"] is None
    assert _row(out, 1)["gap_extended"] is None


def test_both_extended_values_present() -> None:
    ctx = _ctx(10.0, 11.0, [11.0, 10.5, 10.0, 11.5])
    out = run_group(REGISTRY.get_group("gap_fill_state"), ctx)
    vals = set(out["gap_extended"].drop_nulls().to_list())
    assert vals == {0, 1}
