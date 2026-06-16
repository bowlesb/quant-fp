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

import numpy as np

from quantlib.features import BatchContext, REGISTRY, run_group
from quantlib.features.groups.momentum_run import RUN_TOL, SKEW_TOL

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


def _tick_quantized_closes(n: int = 900) -> list[float]:
    """A real-data-regime intraday close path: a near-linear drift plus a small random walk, QUANTIZED to
    the penny tick. The discrete-cent residuals on a near-linear trend are what drive the third-moment
    catastrophic cancellation (the cubed centered-time sums) to round differently between the whole-buffer
    rolling form and the window-sliced live form — exactly the divergence the real ``/store`` audit found."""
    rng = np.random.default_rng(3)
    trend = 100.0 + 0.003 * np.arange(n)
    walk = np.cumsum(rng.standard_normal(n)) * 0.01
    return np.round(trend + walk, 2).tolist()


def test_residual_skew_window_sliced_latest_matches_rolling_on_deep_buffer() -> None:
    """PARITY: on a buffer FAR deeper than the group's window, the window-sliced ``compute_latest`` must
    equal the backfill rolling form's last minute within the DECLARED residual_skew tolerance. The third
    moment is catastrophic-cancellation-prone (cubed centered-time sums), so on tick-quantized real-regime
    prices the two forms round it differently — the float-noise SKEW_TOL (0.02) bounds. This case exceeds
    the old RUN_TOL (1e-4) on several windows, so it genuinely guards the tolerance fix; it stays well
    within SKEW_TOL. (Discovered by the real-data parity audit, quantlib.features.parity_audit.)"""
    ctx = BatchContext(frames={"minute_agg": _frame(_tick_quantized_closes())})
    group = REGISTRY.get_group("momentum_run")
    latest = group.compute(ctx)["minute"].max()
    rolling = group.compute(ctx).filter(pl.col("minute") == latest).sort("symbol")
    live = group.compute_latest(ctx).filter(pl.col("minute") == latest).sort("symbol").select(rolling.columns)
    exceeds_old_tol = False
    for col in SKEW_COLS:
        back, real = rolling[col][0], live[col][0]
        assert (back is None) == (real is None), f"{col}: null-vs-value parity break ({back} vs {real})"
        if back is not None:
            assert abs(back - real) <= 1e-9 + SKEW_TOL * abs(back), (
                f"{col}: window-sliced live {real} != rolling backfill {back} beyond SKEW_TOL={SKEW_TOL}"
            )
            if abs(back - real) > 1e-9 + RUN_TOL * abs(back):
                exceeds_old_tol = True
    assert exceeds_old_tol, "test no longer exercises the third-moment cancellation it guards (would pass at old RUN_TOL)"
