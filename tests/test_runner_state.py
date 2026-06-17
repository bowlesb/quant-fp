"""Unit tests for runner_state — the point-in-time small-cap morning-runner detector.

Hand-built minute_agg + daily frames with a known intraday path lock in the running since-open
state (cum-max high, gap, pullback, dollar-vol, band/active flags). Parity (compute_latest ==
compute on the last minute) is covered by the generic tests/test_fp_latest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

# 13:30 UTC = 09:30 ET (June, EDT = UTC-4) — the session open.
OPEN = datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc)


def _ctx(
    prev_close: float,
    highs: list[float],
    closes: list[float],
    opens0: float,
    vols: list[float],
) -> BatchContext:
    rows = []
    for i, (high, close, vol) in enumerate(zip(highs, closes, vols)):
        rows.append(
            {
                "symbol": "RUN",
                "minute": OPEN + timedelta(minutes=i),
                "open": opens0 if i == 0 else close,
                "high": float(high),
                "close": float(close),
                "volume": float(vol),
            }
        )
    minute = pl.DataFrame(rows)
    # prior trading day's close = prev_close (yesterday); today's daily close is whatever.
    daily = pl.DataFrame(
        {
            "symbol": ["RUN", "RUN"],
            "date": [OPEN.date() - timedelta(days=1), OPEN.date()],
            "close": [prev_close, closes[-1]],
        }
    )
    return BatchContext(frames={"minute_agg": minute, "daily": daily})


def _row(out: pl.DataFrame, minute_idx: int) -> dict:
    return out.filter(pl.col("minute") == OPEN + timedelta(minutes=minute_idx)).row(
        0, named=True
    )


def test_running_high_and_early_move() -> None:
    # prev_close=$4. Highs ramp 5,7,6,8 → running max 5,7,7,8 → early_move 0.25,0.75,0.75,1.0.
    ctx = _ctx(
        4.0,
        highs=[5, 7, 6, 8],
        closes=[4.8, 6.5, 5.5, 7.0],
        opens0=4.2,
        vols=[100, 200, 150, 300],
    )
    out = run_group(REGISTRY.get_group("runner_state"), ctx)
    assert _row(out, 0)["runner_early_move"] == pytest.approx(0.25)
    assert _row(out, 1)["runner_early_move"] == pytest.approx(0.75)
    assert _row(out, 2)["runner_early_move"] == pytest.approx(
        0.75
    )  # running max, NOT this-minute high
    assert _row(out, 3)["runner_early_move"] == pytest.approx(1.0)


def test_gap_and_pullback_from_high() -> None:
    ctx = _ctx(
        4.0,
        highs=[5, 7, 6, 8],
        closes=[4.8, 6.5, 5.5, 7.0],
        opens0=4.2,
        vols=[100, 200, 150, 300],
    )
    out = run_group(REGISTRY.get_group("runner_state"), ctx)
    # gap_open = session_open/prev_close-1 = 4.2/4 - 1, constant all session.
    assert _row(out, 0)["runner_gap_open"] == pytest.approx(0.05)
    assert _row(out, 3)["runner_gap_open"] == pytest.approx(0.05)
    # pullback at minute 2: close 5.5 / running-high 7 - 1.
    assert _row(out, 2)["runner_pullback_from_high"] == pytest.approx(5.5 / 7.0 - 1.0)
    # at the running-high minute (idx1, close 6.5 vs high 7) pullback is negative; idx3 close 7 vs high 8.
    assert _row(out, 3)["runner_pullback_from_high"] == pytest.approx(7.0 / 8.0 - 1.0)


def test_band_and_active_flags_in_band() -> None:
    ctx = _ctx(
        4.0,
        highs=[5, 7, 6, 8],
        closes=[4.8, 6.5, 5.5, 7.0],
        opens0=4.2,
        vols=[100, 200, 150, 300],
    )
    out = run_group(REGISTRY.get_group("runner_state"), ctx)
    # prev_close $4 ∈ [2,20] → in_band 1. early_move >= 0.30 from minute 0 (0.25 < 0.30 at idx0!).
    assert _row(out, 0)["runner_in_band"] == 1
    assert _row(out, 0)["runner_is_active"] == 0  # early_move 0.25 < 0.30
    assert _row(out, 1)["runner_is_active"] == 1  # early_move 0.75 >= 0.30


def test_out_of_band_flag_off() -> None:
    # prev_close $50 → out of [2,20] band → in_band 0, is_active 0 even on a huge move.
    ctx = _ctx(50.0, highs=[80, 90], closes=[75, 85], opens0=55, vols=[100, 200])
    out = run_group(REGISTRY.get_group("runner_state"), ctx)
    assert _row(out, 1)["runner_in_band"] == 0
    assert _row(out, 1)["runner_is_active"] == 0


def test_dollar_vol_accumulates() -> None:
    ctx = _ctx(4.0, highs=[5, 7], closes=[5.0, 6.0], opens0=4.2, vols=[100, 200])
    out = run_group(REGISTRY.get_group("runner_state"), ctx)
    # cumulative dollar vol at minute1 = 5*100 + 6*200 = 1700 → log1p(1700).
    import math

    assert _row(out, 1)["runner_log_dollar_vol"] == pytest.approx(math.log1p(1700.0))


def test_both_flag_values_present() -> None:
    """Golden-set binary rule: both 0 and 1 must occur for the Int8 flags."""
    ctx = _ctx(
        4.0,
        highs=[5, 7, 6, 8],
        closes=[4.8, 6.5, 5.5, 7.0],
        opens0=4.2,
        vols=[100, 200, 150, 300],
    )
    out = run_group(REGISTRY.get_group("runner_state"), ctx)
    active_vals = set(out["runner_is_active"].drop_nulls().to_list())
    assert active_vals == {0, 1}
