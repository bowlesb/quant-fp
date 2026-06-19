"""Unit tests for draw_range — total path excursion (max draw-down + max draw-up) over the window.

Hand-built minute_agg with a known price path pins the excursion math + the degenerate-price guard + the
warmup edge. Live==backfill parity for this compute_latest_on_window group is covered by the shared
tests/test_fp_latest.py and reconfirmed directly below.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _minute_agg(closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(closes),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(closes))],
            "close": closes,
        }
    )


def _row(out: pl.DataFrame, i: int) -> dict:
    return out.filter(pl.col("minute") == BASE + timedelta(minutes=i)).row(0, named=True)


def _run(frame: pl.DataFrame) -> pl.DataFrame:
    return run_group(REGISTRY.get_group("draw_range"), BatchContext(frames={"minute_agg": frame}))


def test_excursion_known_value() -> None:
    # Path 100 -> 105 -> 110 (peak) -> 102 -> 99 (trough).
    #   max draw-up: highest close/running-min - 1 = 110/100 - 1 = 0.10.
    #   max draw-down: lowest close/running-max - 1 = 99/110 - 1 = -0.0909..., |.| = 0.0909...
    out = _run(_minute_agg([100.0, 105.0, 110.0, 102.0, 99.0]))
    drawup = 110.0 / 100.0 - 1.0
    drawdown = abs(99.0 / 110.0 - 1.0)
    row = _row(out, 4)
    assert row["max_drawup_60m"] == pytest.approx(drawup, rel=1e-9)
    assert row["max_drawdown_60m"] == pytest.approx(drawdown, rel=1e-9)
    assert row["draw_range_60m"] == pytest.approx(drawup + drawdown, rel=1e-9)


def test_monotone_up_has_no_drawdown() -> None:
    # Strictly rising path: draw-down is 0 (close never below running max), draw-up = total rise.
    row = _row(_run(_minute_agg([100.0, 110.0, 120.0])), 2)
    assert row["max_drawdown_60m"] == pytest.approx(0.0, abs=1e-12)
    assert row["max_drawup_60m"] == pytest.approx(120.0 / 100.0 - 1.0, rel=1e-9)
    assert row["draw_range_60m"] == pytest.approx(120.0 / 100.0 - 1.0, rel=1e-9)


def test_monotone_down_has_no_drawup() -> None:
    # Strictly falling path: draw-up is 0 (close never above running min), draw-down = total fall.
    row = _row(_run(_minute_agg([100.0, 90.0, 80.0])), 2)
    assert row["max_drawup_60m"] == pytest.approx(0.0, abs=1e-12)
    assert row["max_drawdown_60m"] == pytest.approx(abs(80.0 / 100.0 - 1.0), rel=1e-9)
    assert row["draw_range_60m"] == pytest.approx(abs(80.0 / 100.0 - 1.0), rel=1e-9)


def test_first_minute_is_warmup_null() -> None:
    # A single close has no excursion (< MIN_POINTS) -> null.
    out = _run(_minute_agg([100.0]))
    assert _row(out, 0)["draw_range_60m"] is None


def test_non_positive_close_does_not_break() -> None:
    # A degenerate zero-price bar is guarded (its per-bar excursion is null), the rest is well-defined.
    out = _run(_minute_agg([100.0, 0.0, 110.0]))
    assert _row(out, 2)["draw_range_60m"] is not None


def test_compute_latest_matches_backfill_last() -> None:
    closes = [100.0, 105.0, 110.0, 102.0, 99.0, 101.0]
    group = REGISTRY.get_group("draw_range")
    ctx = BatchContext(frames={"minute_agg": _minute_agg(closes)})
    rolling = group.compute(ctx)
    last = rolling["minute"].max()
    expected = rolling.filter(pl.col("minute") == last).row(0, named=True)["draw_range_60m"]
    actual = group.compute_latest(ctx).row(0, named=True)["draw_range_60m"]
    assert actual == pytest.approx(expected, rel=1e-9)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
