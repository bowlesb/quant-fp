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
from quantlib.features.groups.momentum_run import LOOKBACK_MINUTES, SKEW_TOL

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
    the penny tick. The discrete-cent residuals on a near-linear trend are the regime that USED to drive the
    old third-moment formulation's catastrophic cancellation (cubed centered-time power sums); the window-local
    fit has no cross-window origin and no cancellation, so this case now agrees tightly."""
    rng = np.random.default_rng(3)
    trend = 100.0 + 0.003 * np.arange(n)
    walk = np.cumsum(rng.standard_normal(n)) * 0.01
    return np.round(trend + walk, 2).tolist()


def test_residual_skew_window_sliced_latest_matches_rolling_on_deep_buffer() -> None:
    """PARITY: on a buffer FAR deeper than the group's window, the window-sliced ``compute_latest`` must equal
    the backfill rolling form's last minute within the DECLARED residual_skew tolerance. The window-local fit
    (each window's own bars on a window-relative axis) is the IDENTICAL computation in both paths, so on the
    tick-quantized real-regime path that broke the old power-sum form they now agree to summation float-noise —
    well within SKEW_TOL."""
    ctx = BatchContext(frames={"minute_agg": _frame(_tick_quantized_closes())})
    group = REGISTRY.get_group("momentum_run")
    latest = group.compute(ctx)["minute"].max()
    rolling = group.compute(ctx).filter(pl.col("minute") == latest).sort("symbol")
    live = group.compute_latest(ctx).filter(pl.col("minute") == latest).sort("symbol").select(rolling.columns)
    for col in SKEW_COLS:
        back, real = rolling[col][0], live[col][0]
        assert (back is None) == (real is None), f"{col}: null-vs-value parity break ({back} vs {real})"
        if back is not None:
            assert abs(back - real) <= 1e-9 + SKEW_TOL * abs(back), (
                f"{col}: window-sliced live {real} != rolling backfill {back} beyond SKEW_TOL={SKEW_TOL}"
            )


def test_residual_skew_is_point_in_time_no_lookahead() -> None:
    """LOOK-AHEAD: residual_skew at minute T must be byte-identical whether the buffer ends at T or extends
    far past it. The old buffer-relative power-sum form FAILED this (appending future bars shifted the time
    origin and re-rounded the cancellation-prone cubed sums, drifting past values ~1e-8). The window-local
    fit depends only on the bars in (T−W, T], so future data cannot touch it."""
    closes = _tick_quantized_closes(260)
    group = REGISTRY.get_group("momentum_run")
    cutoff = BASE + timedelta(minutes=199)
    partial = group.compute(BatchContext(frames={"minute_agg": _frame(closes[:200])}))
    full = group.compute(BatchContext(frames={"minute_agg": _frame(closes)}))
    before = partial.filter(pl.col("minute") <= cutoff).sort("minute").select(["minute", *SKEW_COLS])
    after = full.filter(pl.col("minute") <= cutoff).sort("minute").select(["minute", *SKEW_COLS])
    assert before.height > 0
    assert before.equals(after), "residual_skew changed at minutes <= T when future bars were appended"


def _multi(closes_by_symbol: dict[str, list[float]]) -> pl.DataFrame:
    """A multi-symbol minute_agg frame; each symbol's closes laid on the same contiguous minute grid."""
    rows_symbol: list[str] = []
    rows_minute: list[datetime] = []
    rows_close: list[float] = []
    for symbol, closes in closes_by_symbol.items():
        for i, close in enumerate(closes):
            rows_symbol.append(symbol)
            rows_minute.append(BASE + timedelta(minutes=i))
            rows_close.append(close)
    return pl.DataFrame({"symbol": rows_symbol, "minute": rows_minute, "close": rows_close})


def test_streak_split_does_not_recompute_skew_but_stays_identical() -> None:
    """compute_latest computes longest_streak directly (not via the full compute(), which would redundantly
    recompute the residual_skew gather on the streak slice). Pin that the split is value-identical: the streak
    columns from compute_latest equal the backfill compute().last cell-for-cell, multi-symbol with distinct run
    structures."""
    rng = np.random.default_rng(5)
    closes = {
        "AAA": (100.0 + np.cumsum(rng.standard_normal(80) * 0.05)).tolist(),
        "BBB": [100.0 + 0.1 * i for i in range(80)],  # a pure up-run (long streak)
        "CCC": (200.0 + np.cumsum(rng.choice([-0.1, 0.1], 80))).tolist(),
    }
    ctx = BatchContext(frames={"minute_agg": _multi(closes)})
    group = REGISTRY.get_group("momentum_run")
    backfill = group.compute(ctx)
    last = backfill["minute"].max()
    bf = backfill.filter(pl.col("minute") == last).sort("symbol")
    live = group.compute_latest(ctx).filter(pl.col("minute") == last).sort("symbol").select(bf.columns)
    streak_cols = [f"longest_streak_{w}m" for w in (5, 10, 15, 20, 30, 60)]
    for col in streak_cols:
        for back, real in zip(bf[col].to_list(), live[col].to_list()):
            assert (back is None) == (real is None), f"{col}: null parity break ({back} vs {real})"
            if back is not None:
                assert back == real, f"{col}: streak split {real} != backfill {back}"


def test_streak_split_sparse_deep_buffer_identical() -> None:
    """The split streak path (compute_latest computing longest_streak directly via _compute_streak on the
    prior-bar-extended slice) stays cell-for-cell == backfill on a DEEP, GAPPY multi-symbol buffer — a sparse
    symbol whose in-window earliest bar's positional predecessor sits an arbitrary gap back, on a buffer deeper
    than the LOOKBACK slice. (longest_streak caps each run at its in-window position, so it is robust to the
    boundary return either way; this pins that the split did not perturb the streak values regardless.)"""
    span = LOOKBACK_MINUTES + 50  # buffer deeper than the slice so a pre-slice predecessor is possible
    rng = np.random.default_rng(9)
    dense = (100.0 + np.cumsum(rng.standard_normal(span) * 0.04)).tolist()
    rows = []
    for i in range(span):
        rows.append(("AAA", BASE + timedelta(minutes=i), dense[i]))
    # BBB: one early bar (well before the LOOKBACK slice), then a contiguous run inside the slice. BBB's first
    # in-slice bar's positional predecessor is that early bar — an arbitrary gap back, OUTSIDE the slice.
    early_minute = 2
    rows.append(("BBB", BASE + timedelta(minutes=early_minute), 200.0))
    in_window_start = span - 40  # contiguous BBB bars inside the trailing LOOKBACK window
    bbb = 200.0 + np.cumsum(rng.choice([-0.1, 0.1], 40))
    for offset in range(40):
        rows.append(("BBB", BASE + timedelta(minutes=in_window_start + offset), float(bbb[offset])))
    frame = pl.DataFrame(
        {"symbol": [r[0] for r in rows], "minute": [r[1] for r in rows], "close": [r[2] for r in rows]}
    ).sort(["symbol", "minute"])
    ctx = BatchContext(frames={"minute_agg": frame})
    group = REGISTRY.get_group("momentum_run")
    backfill = group.compute(ctx)
    last = backfill["minute"].max()
    bf = backfill.filter(pl.col("minute") == last).sort("symbol")
    live = group.compute_latest(ctx).filter(pl.col("minute") == last).sort("symbol").select(bf.columns)
    for col in [f"longest_streak_{w}m" for w in (5, 10, 15, 20, 30, 60)]:
        for back, real in zip(bf[col].to_list(), live[col].to_list()):
            assert (back is None) == (real is None), f"{col}: null parity break ({back} vs {real})"
            if back is not None:
                assert back == real, f"{col}: sparse streak split {real} != backfill {back}"
