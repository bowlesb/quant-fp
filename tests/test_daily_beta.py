"""Unit tests for daily_beta — the rolling 60-day beta/corr/idio-vol of DAILY returns to SPY.

Hand-built daily + minute_agg frames with a known beta (a name whose daily returns are exactly k×
SPY's → beta k, corr 1) lock in the OLS math. Parity (compute_latest == compute on the last minute)
is covered by the generic tests/test_fp_latest.py.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import polars as pl
import pytest

from quantlib.features import BatchContext, REGISTRY, run_group

WINDOW = 70  # > 60 so the 60d rolling beta is fully warm on the last day
BASE_DATE = date(2026, 3, 1)
LAST_MINUTE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _daily_panel() -> pl.DataFrame:
    """SPY with random-ish daily returns; AAA = exactly 2x SPY's return (beta 2, corr 1);
    BBB = 0.5x SPY (beta 0.5). Closes are reconstructed from the return paths."""
    rng = np.random.default_rng(7)
    spy_rets = rng.normal(0.0, 0.01, WINDOW)
    rows = []
    specs = {"SPY": 1.0, "AAA": 2.0, "BBB": 0.5}
    for sym, k in specs.items():
        price = 100.0
        for i in range(WINDOW):
            ret = spy_rets[i] * k if i > 0 else 0.0
            price *= 1.0 + ret
            rows.append(
                {
                    "symbol": sym,
                    "date": BASE_DATE + timedelta(days=i),
                    "close": float(price),
                }
            )
    return pl.DataFrame(rows)


def _ctx() -> BatchContext:
    daily = _daily_panel()
    # minute_agg: one minute on the LAST daily date (so the broadcast lands on a real day).
    last_date = BASE_DATE + timedelta(days=WINDOW - 1)
    last_min = datetime(
        last_date.year, last_date.month, last_date.day, 14, 0, tzinfo=timezone.utc
    )
    minute = pl.DataFrame(
        {"symbol": ["AAA", "BBB", "SPY"], "minute": [last_min, last_min, last_min]}
    )
    return BatchContext(frames={"daily": daily, "minute_agg": minute})


def _row(out: pl.DataFrame, sym: str) -> dict:
    return out.filter(pl.col("symbol") == sym).row(0, named=True)


def test_beta_recovers_known_slope() -> None:
    out = run_group(REGISTRY.get_group("daily_beta"), _ctx())
    # AAA is exactly 2x SPY → beta ~2; BBB 0.5x → beta ~0.5.
    assert _row(out, "AAA")["daily_beta_60d"] == pytest.approx(2.0, abs=1e-6)
    assert _row(out, "BBB")["daily_beta_60d"] == pytest.approx(0.5, abs=1e-6)


def test_corr_is_one_for_linear_name() -> None:
    out = run_group(REGISTRY.get_group("daily_beta"), _ctx())
    # a pure linear multiple of SPY is perfectly correlated.
    assert _row(out, "AAA")["daily_corr_60d"] == pytest.approx(1.0, abs=1e-6)
    assert _row(out, "BBB")["daily_corr_60d"] == pytest.approx(1.0, abs=1e-6)


def test_idio_vol_zero_for_linear_name() -> None:
    out = run_group(REGISTRY.get_group("daily_beta"), _ctx())
    # corr == 1 → sqrt(1 - corr^2) == 0 → no idiosyncratic vol.
    assert _row(out, "AAA")["daily_idio_vol_60d"] == pytest.approx(0.0, abs=1e-9)


def test_spy_beta_is_one() -> None:
    out = run_group(REGISTRY.get_group("daily_beta"), _ctx())
    # SPY regressed on itself → beta 1, corr 1.
    assert _row(out, "SPY")["daily_beta_60d"] == pytest.approx(1.0, abs=1e-6)
