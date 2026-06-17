"""Unit tests for overnight_beta — the W11 overnight-vs-intraday beta decomposition.

Hand-built daily panel where a name's OVERNIGHT return is 2x SPY's and its INTRADAY return is 0.5x
SPY's, so the two legs have KNOWN, DIFFERENT betas (the asymmetry). Parity is covered by the generic
tests/test_fp_latest.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

WINDOW = 70
BASE_DATE = date(2026, 3, 1)


def _daily_panel() -> pl.DataFrame:
    """SPY with random overnight + intraday legs. AAA: overnight = 2x SPY-overnight, intraday = 0.5x
    SPY-intraday → overnight_beta 2, intraday_beta 0.5. Reconstruct open/close from the two legs.
    """
    rng = np.random.default_rng(11)
    spy_on = rng.normal(0.0, 0.008, WINDOW)
    spy_id = rng.normal(0.0, 0.008, WINDOW)
    rows = []
    specs = {"SPY": (1.0, 1.0), "AAA": (2.0, 0.5)}
    for sym, (k_on, k_id) in specs.items():
        prev_close = 100.0
        for i in range(WINDOW):
            on_ret = spy_on[i] * k_on if i > 0 else 0.0
            id_ret = spy_id[i] * k_id if i > 0 else 0.0
            open_px = prev_close * (1.0 + on_ret)
            close_px = open_px * (1.0 + id_ret)
            rows.append(
                {
                    "symbol": sym,
                    "date": BASE_DATE + timedelta(days=i),
                    "open": float(open_px),
                    "close": float(close_px),
                }
            )
            prev_close = close_px
    return pl.DataFrame(rows)


def _ctx() -> BatchContext:
    daily = _daily_panel()
    last_date = BASE_DATE + timedelta(days=WINDOW - 1)
    last_min = datetime(
        last_date.year, last_date.month, last_date.day, 14, 0, tzinfo=timezone.utc
    )
    minute = pl.DataFrame({"symbol": ["AAA", "SPY"], "minute": [last_min, last_min]})
    return BatchContext(frames={"daily": daily, "minute_agg": minute})


def _row(out: pl.DataFrame, sym: str) -> dict:
    return out.filter(pl.col("symbol") == sym).row(0, named=True)


def test_overnight_and_intraday_legs_recover() -> None:
    out = run_group(REGISTRY.get_group("overnight_beta"), _ctx())
    row = _row(out, "AAA")
    assert row["overnight_beta_60d"] == pytest.approx(2.0, abs=1e-6)
    assert row["intraday_beta_60d"] == pytest.approx(0.5, abs=1e-6)


def test_asymmetry_is_difference() -> None:
    out = run_group(REGISTRY.get_group("overnight_beta"), _ctx())
    row = _row(out, "AAA")
    # asymmetry = overnight - intraday = 2.0 - 0.5 = 1.5 (this name carries more market risk overnight).
    assert row["beta_overnight_minus_intraday"] == pytest.approx(1.5, abs=1e-6)


def test_spy_legs_are_one() -> None:
    out = run_group(REGISTRY.get_group("overnight_beta"), _ctx())
    row = _row(out, "SPY")
    assert row["overnight_beta_60d"] == pytest.approx(1.0, abs=1e-6)
    assert row["intraday_beta_60d"] == pytest.approx(1.0, abs=1e-6)
    assert row["beta_overnight_minus_intraday"] == pytest.approx(0.0, abs=1e-6)
