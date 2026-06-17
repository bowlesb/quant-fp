"""Unit tests for dumper_state — the point-in-time small-cap morning-DUMPER detector (F9 mirror).

Hand-built minute_agg + daily frames with a known intraday path lock in the running since-open
state (cum-min low, gap, bounce, dollar-vol, band/active flags). Parity (compute_latest ==
compute on the last minute) is covered by the generic tests/test_fp_latest.py.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

# 13:30 UTC = 09:30 ET (June, EDT = UTC-4) — the session open.
OPEN = datetime(2026, 6, 12, 13, 30, tzinfo=timezone.utc)


def _ctx(
    prev_close: float,
    lows: list[float],
    closes: list[float],
    opens0: float,
    vols: list[float],
) -> BatchContext:
    rows = []
    for i, (low, close, vol) in enumerate(zip(lows, closes, vols)):
        rows.append(
            {
                "symbol": "DMP",
                "minute": OPEN + timedelta(minutes=i),
                "open": opens0 if i == 0 else close,
                "low": float(low),
                "close": float(close),
                "volume": float(vol),
            }
        )
    minute = pl.DataFrame(rows)
    daily = pl.DataFrame(
        {
            "symbol": ["DMP", "DMP"],
            "date": [OPEN.date() - timedelta(days=1), OPEN.date()],
            "close": [prev_close, closes[-1]],
        }
    )
    return BatchContext(frames={"minute_agg": minute, "daily": daily})


def _row(out: pl.DataFrame, minute_idx: int) -> dict:
    return out.filter(pl.col("minute") == OPEN + timedelta(minutes=minute_idx)).row(
        0, named=True
    )


def test_running_low_and_early_drop() -> None:
    # prev_close=$10. Lows fall 8,6,7,5 → running min 8,6,6,5 → early_drop 0.2,0.4,0.4,0.5.
    ctx = _ctx(
        10.0,
        lows=[8, 6, 7, 5],
        closes=[8.5, 6.5, 7.5, 5.5],
        opens0=9.0,
        vols=[100, 200, 150, 300],
    )
    out = run_group(REGISTRY.get_group("dumper_state"), ctx)
    assert _row(out, 0)["dumper_early_drop"] == pytest.approx(0.2)
    assert _row(out, 1)["dumper_early_drop"] == pytest.approx(0.4)
    assert _row(out, 2)["dumper_early_drop"] == pytest.approx(
        0.4
    )  # running MIN, not this-minute low
    assert _row(out, 3)["dumper_early_drop"] == pytest.approx(0.5)


def test_gap_and_bounce_from_low() -> None:
    ctx = _ctx(
        10.0,
        lows=[8, 6, 7, 5],
        closes=[8.5, 6.5, 7.5, 5.5],
        opens0=9.0,
        vols=[100, 200, 150, 300],
    )
    out = run_group(REGISTRY.get_group("dumper_state"), ctx)
    # gap_open = session_open/prev_close-1 = 9/10 - 1 = -0.1, constant.
    assert _row(out, 0)["dumper_gap_open"] == pytest.approx(-0.1)
    assert _row(out, 3)["dumper_gap_open"] == pytest.approx(-0.1)
    # bounce at minute 2: close 7.5 / running-low 6 - 1.
    assert _row(out, 2)["dumper_bounce_from_low"] == pytest.approx(7.5 / 6.0 - 1.0)
    # at minute 3 close 5.5 vs running-low 5.
    assert _row(out, 3)["dumper_bounce_from_low"] == pytest.approx(5.5 / 5.0 - 1.0)


def test_band_and_active_flags_in_band() -> None:
    ctx = _ctx(
        10.0,
        lows=[8, 6, 7, 5],
        closes=[8.5, 6.5, 7.5, 5.5],
        opens0=9.0,
        vols=[100, 200, 150, 300],
    )
    out = run_group(REGISTRY.get_group("dumper_state"), ctx)
    assert _row(out, 0)["dumper_in_band"] == 1
    assert _row(out, 0)["dumper_is_active"] == 0  # early_drop 0.2 < 0.30
    assert _row(out, 1)["dumper_is_active"] == 1  # early_drop 0.4 >= 0.30


def test_out_of_band_flag_off() -> None:
    # prev_close $50 → out of [2,20] band → flags 0 even on a huge drop.
    ctx = _ctx(50.0, lows=[30, 20], closes=[32, 22], opens0=45, vols=[100, 200])
    out = run_group(REGISTRY.get_group("dumper_state"), ctx)
    assert _row(out, 1)["dumper_in_band"] == 0
    assert _row(out, 1)["dumper_is_active"] == 0


def test_dollar_vol_accumulates() -> None:
    ctx = _ctx(10.0, lows=[8, 6], closes=[8.0, 7.0], opens0=9.0, vols=[100, 200])
    out = run_group(REGISTRY.get_group("dumper_state"), ctx)
    # cumulative dollar vol at minute1 = 8*100 + 7*200 = 2200 → log1p(2200).
    assert _row(out, 1)["dumper_log_dollar_vol"] == pytest.approx(math.log1p(2200.0))


def test_both_flag_values_present() -> None:
    ctx = _ctx(
        10.0,
        lows=[8, 6, 7, 5],
        closes=[8.5, 6.5, 7.5, 5.5],
        opens0=9.0,
        vols=[100, 200, 150, 300],
    )
    out = run_group(REGISTRY.get_group("dumper_state"), ctx)
    active_vals = set(out["dumper_is_active"].drop_nulls().to_list())
    assert active_vals == {0, 1}
