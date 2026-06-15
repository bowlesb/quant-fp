"""Regression tests for momentum_run.residual_skew degenerate-spread guard.

residual_skew = m3 / m2**1.5 where m2 is the OLS residual VARIANCE. On a near-perfectly-linear price
path the true residual spread collapses to floating-point cancellation noise: m2 stays positive but is
dominated by roundoff, so the ratio explodes (observed live: residual_skew up to +/-1.6e9 vs the declared
+/-20 range, breaching valid_range for ~0.5% of 5m rows). The fix gates on a RELATIVE residual-spread floor
(residual std must exceed REL_RESID_FLOOR of the window's mean price). These tests pin that the degenerate
case nulls out and the healthy case is NOT over-nulled and stays in range.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features import BatchContext, REGISTRY, run_group

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
SKEW_COLS = [f"residual_skew_{w}m" for w in (5, 10, 15, 20, 30, 60)]


def _frame(closes: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AAA"] * len(closes),
            "minute": [BASE + timedelta(minutes=i) for i in range(len(closes))],
            "close": closes,
        }
    )


def test_residual_skew_near_linear_path_is_nulled_not_blown_up() -> None:
    """A path that is linear to within ~1e-7 of the price level has no trustworthy residual shape.
    The guard nulls it (instead of returning a cancellation-driven blowup), and run_group's range
    check (validate=True) does NOT raise."""
    # close = linear ramp + tiny asymmetric wiggle ~1e-5 absolute on a ~$100 stock => relative residual
    # std ~1e-7, well below the 1e-6 floor. True residual dominates roundoff, so this is deterministic.
    wiggle = [-1e-5, -1e-5, -1e-5, -1e-5, 4e-5]
    closes = [100.0 + 0.3 * i + wiggle[i % len(wiggle)] for i in range(70)]
    # validate=True => raises if any residual_skew breaches (-20, 20); old code could blow up here.
    out = run_group(REGISTRY.get_group("momentum_run"), BatchContext(frames={"minute_agg": _frame(closes)}))
    # every residual_skew value is null (degenerate spread gated) -- no finite, untrustworthy blowup leaks
    for col in SKEW_COLS:
        non_null = out[col].drop_nulls()
        assert non_null.len() == 0, f"{col}: expected all-null on a sub-floor near-linear path, got {non_null.to_list()[:5]}"


def test_residual_skew_healthy_path_in_range_not_overnulled() -> None:
    """A path with a genuine residual spread (>> the floor) keeps producing finite, in-range skew --
    the floor must not over-null real data."""
    resid = [-0.20, -0.15, -0.18, -0.22, 0.75]  # right-skewed residuals, std ~0.4 on a ~$110 stock
    closes = [100.0 + 0.2 * i + resid[i % len(resid)] for i in range(70)]
    out = run_group(REGISTRY.get_group("momentum_run"), BatchContext(frames={"minute_agg": _frame(closes)}))
    last = out.filter(pl.col("minute") == BASE + timedelta(minutes=69)).row(0, named=True)
    # the fully-warmed long window produces a real value, in range
    assert last["residual_skew_60m"] is not None
    assert -20.0 < last["residual_skew_60m"] < 20.0
    # the group as a whole is not silently all-null (guard didn't over-null healthy data)
    total_non_null = sum(out[col].drop_nulls().len() for col in SKEW_COLS)
    assert total_non_null > 0
