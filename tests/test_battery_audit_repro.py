"""Adversarial-audit repro tests (BatteryAudit, 2026-06-19) — now the REGRESSION GUARDS for the fixes.

Each test pins a confirmed defect the audit found; the fixes have landed, so these now PASS and guard
against regression.

Findings (fixed):
  F1 — intraday `_forward_excess` applied NO $1 price-integrity floor on the FORWARD leg, while the
       module docstring + daily path claim the floor is on BOTH legs. A penny-print 30m forward
       leaked a ~-100% return into the cross-sectional label. FIX: both legs floored.
  F2 — the EOD `up_down_market` conditioner selected rows on `up_market_day` = sign of TODAY's
       open->close median, unknown at the 09:35 EOD entry (a look-ahead). FIX: EOD conditions on the
       PRIOR day's regime (the same shift the EOD features get).
  F3 — the raw fast path (use_gbm=False) ranked feature COLUMN 0 ONLY, so an "empty leaderboard" meant
       "column 0 has no edge", not "no feature in the set has edge" (a false null). FIX: the fast path
       scores an equal-weight composite of ALL columns.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.battery.panel import Panel, _forward_excess
from quantlib.battery.spec import ArchetypeSpec, Conditioner, Horizon, Sizing
from quantlib.battery.strategy import CrossSectionalLS, _ratio_with_floor


def test_intraday_forward_leg_applies_dollar_floor() -> None:
    """A penny-print FORWARD bar must be nulled the same way the daily path nulls it via
    `_ratio_with_floor`. Today it leaks a ~-100% return into the label."""
    day = dt.date(2025, 1, 6)
    t0 = dt.datetime(day.year, day.month, day.day, 14, 35, tzinfo=dt.timezone.utc)
    t1 = t0 + dt.timedelta(minutes=30)
    rows = []
    for i in range(21):  # >= MIN_CROSS_SECTION breadth so the minute is graded
        rows.append({"symbol": f"N{i}", "ts": t0, "close": 50.0})
        rows.append({"symbol": f"N{i}", "ts": t1, "close": 50.0 * (1 + 0.001 * (i - 10))})
    rows.append({"symbol": "PENNY", "ts": t0, "close": 50.0})  # entry $50 passes the entry floor
    rows.append({"symbol": "PENNY", "ts": t1, "close": 0.02})  # forward $0.02 penny print
    bars = pl.DataFrame(rows).with_columns(pl.col("ts").dt.replace_time_zone("UTC"))
    fwd = _forward_excess(bars, pl.Series([t0]), 30)
    penny_label = fwd.filter(pl.col("symbol") == "PENNY")["fwd_30m"].to_list()[0]
    # the daily path would floor this to NaN:
    assert np.isnan(_ratio_with_floor(np.array([0.02]), np.array([50.0]))[0])
    # the intraday path SHOULD too — this is the assertion that currently fails:
    assert penny_label is None or np.isnan(
        penny_label
    ), f"penny-print forward leg leaked label {penny_label} (no $1 floor on the forward leg)"


def test_eod_conditioner_is_point_in_time() -> None:
    """`up_market_day` is the sign of today's open->close median — a FUTURE quantity at the 09:35 EOD
    entry. The EOD conditioner mask must not equal that same-day array (it must be prior-day, like the
    features are). Today it does, so the eod|up_down_market cell conditions on look-ahead."""
    from quantlib.battery.panel import Panel  # local import keeps the repro self-contained

    n = 30
    sc = (np.arange(n) % 5).astype(np.int64)
    mn = np.full(
        n, int(dt.datetime(2025, 1, 6, 19, 59, tzinfo=dt.timezone.utc).timestamp() * 1e9), dtype=np.int64
    )
    up = np.array([True] * (n // 2) + [False] * (n - n // 2))
    panel = Panel(
        symbol_code=sc,
        symbol_names=[f"S{i}" for i in range(5)],
        minute_epoch=mn,
        feature_names=["f0"],
        feature_matrix=np.zeros((n, 1)),
        entry_close=np.full(n, 50.0),
        half_spread_bps=np.full(n, 3.0),
        high=np.full(n, 51.0),
        low=np.full(n, 49.0),
        volume=np.full(n, 1e6),
        extra={
            "exec_0935": np.full(n, 49.0),
            "rth_close": np.full(n, 50.0),
            "exit_overnight": np.full(n, 50.0),
            "exit_2d": np.full(n, 50.0),
            "exit_3d": np.full(n, 50.0),
            "rth_dollar_vol": np.full(n, 1e8),
            "up_market_day": up,
        },
        cadence="daily",
    )
    strat = CrossSectionalLS(ArchetypeSpec("cross_sectional_ls", Horizon.EOD, Conditioner.UP_DOWN_MARKET))
    mask = strat._conditioner_mask(panel)
    assert not np.array_equal(mask, up), "EOD conditioner selects on today's realized direction (look-ahead)"
    # the prior-day shift also means the FIRST row per symbol (no prior) is never selected:
    first_rows = np.array([True] + [sc[i] != sc[i - 1] for i in range(1, n)])
    assert not mask[first_rows].any(), "first row per symbol has no prior-day regime -> must not select"


def _intraday_panel_signal_in_column(signal_col: int, n_cols: int, n_days: int, n_syms: int) -> Panel:
    """An intraday panel where ONLY `signal_col` carries the forward signal; all other columns are
    pure noise. The forward-30m label IS that column's value (so a harness that ranks the signal
    column finds a strong edge; one that ranks only column 0 finds nothing)."""
    rng = np.random.default_rng(5)
    sc, mn, feats, labels = [], [], [], []
    base = dt.datetime(2025, 1, 6, 14, 35, tzinfo=dt.timezone.utc)
    for day in range(n_days):
        minute = int((base + dt.timedelta(days=day)).timestamp() * 1e9)
        raw = rng.normal(0, 1, n_syms)
        excess = raw - np.median(raw)
        for sym in range(n_syms):
            row = rng.normal(0, 1, n_cols)
            row[signal_col] = excess[sym]  # the signal lives in signal_col only
            sc.append(sym)
            mn.append(minute)
            feats.append(row)
            labels.append(float(excess[sym]))
    order = sorted(range(len(sc)), key=lambda i: (sc[i], mn[i]))
    fm = np.array([feats[i] for i in order])
    lab = np.array([labels[i] for i in order])
    n = len(order)
    return Panel(
        symbol_code=np.array([sc[i] for i in order], dtype=np.int64),
        symbol_names=[f"S{i}" for i in range(n_syms)],
        minute_epoch=np.array([mn[i] for i in order], dtype=np.int64),
        feature_names=[f"f{c}" for c in range(n_cols)],
        feature_matrix=fm,
        entry_close=np.full(n, 50.0),
        half_spread_bps=np.full(n, 3.0),
        high=np.full(n, 51.0),
        low=np.full(n, 49.0),
        volume=np.full(n, 1e6),
        extra={"fwd_30m": lab},
        cadence="intraday",
    )


def test_fast_path_tests_full_feature_set_not_column_zero() -> None:
    """F3: the raw fast path must test the WHOLE feature set. With the signal in a NON-zero column,
    the OLD col-0-only fast path found nothing (a FALSE null); the composite fast path must detect it
    so an empty leaderboard honestly means 'no feature in the set carries signal'."""
    spec = ArchetypeSpec("cross_sectional_ls", Horizon.M30, Conditioner.NONE, Sizing.EW)
    # signal in column 3 of 5; column 0 is pure noise.
    panel = _intraday_panel_signal_in_column(signal_col=3, n_cols=5, n_days=40, n_syms=80)
    result = CrossSectionalLS(spec, seed=13, use_gbm=False).backtest(panel)
    # the composite (mean-z over all 5 cols) recovers the column-3 signal -> a clearly positive IC.
    assert (
        result.mean_ic > 0.1
    ), f"fast path missed a signal in column 3 (IC={result.mean_ic:.4f}); it is under-testing the set"


def test_fast_path_pure_noise_is_still_null() -> None:
    """The composite fast path must NOT manufacture edge from a fully-noise set (the null stays a null
    when there is genuinely nothing)."""
    spec = ArchetypeSpec("cross_sectional_ls", Horizon.M30, Conditioner.NONE, Sizing.EW)
    rng = np.random.default_rng(9)
    n_days, n_syms, n_cols = 40, 80, 5
    sc, mn, feats, labels = [], [], [], []
    base = dt.datetime(2025, 1, 6, 14, 35, tzinfo=dt.timezone.utc)
    for day in range(n_days):
        minute = int((base + dt.timedelta(days=day)).timestamp() * 1e9)
        raw = rng.normal(0, 1, n_syms)
        excess = raw - np.median(raw)
        for sym in range(n_syms):
            sc.append(sym)
            mn.append(minute)
            feats.append(rng.normal(0, 1, n_cols))  # features INDEPENDENT of the label
            labels.append(float(excess[sym]))
    order = sorted(range(len(sc)), key=lambda i: (sc[i], mn[i]))
    panel = Panel(
        symbol_code=np.array([sc[i] for i in order], dtype=np.int64),
        symbol_names=[f"S{i}" for i in range(n_syms)],
        minute_epoch=np.array([mn[i] for i in order], dtype=np.int64),
        feature_names=[f"f{c}" for c in range(n_cols)],
        feature_matrix=np.array([feats[i] for i in order]),
        entry_close=np.full(len(order), 50.0),
        half_spread_bps=np.full(len(order), 3.0),
        high=np.full(len(order), 51.0),
        low=np.full(len(order), 49.0),
        volume=np.full(len(order), 1e6),
        extra={"fwd_30m": np.array([labels[i] for i in order])},
        cadence="intraday",
    )
    result = CrossSectionalLS(spec, seed=13, use_gbm=False).backtest(panel)
    assert abs(result.mean_ic) < 0.05, f"composite manufactured edge from noise (IC={result.mean_ic:.4f})"
