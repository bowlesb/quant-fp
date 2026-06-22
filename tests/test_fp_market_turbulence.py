"""market_turbulence parity + math — a universe-wide GATHER reduce broadcast to every ticker.

market_turbulence collapses the cross-section at each minute to a few realized-move-magnitude SCALARS
(``mkt_absret_{5,15,30,60}m`` = universe mean |trailing-W return|; ``mkt_rv_30m`` = universe mean
trailing-30m realized vol) and broadcasts them to every symbol — the same per-minute universe reduce as
``breadth``, so its live aggregate-at-T form must equal its backfill rolling form cell-for-cell. These
tests prove (a) the value is broadcast IDENTICALLY across the universe at a minute (the GATHER property),
(b) the |return| mean matches a hand computation including null-lag exclusion from the denominator, (c)
the realized-vol mean matches a direct std over the trailing-30m 1m log returns, and (d) ``compute_latest``
== ``compute()`` filtered to the last minute (the generic latest-parity contract).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import polars as pl

import pytest

from quantlib.features import REGISTRY
from quantlib.features.base import BatchContext
from quantlib.features.groups.market_turbulence import (
    ABSRET_WINDOWS,
    RV_MIN_OBS,
    RV_WINDOW,
)

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
FEATURE_NAMES = [f"mkt_absret_{w}m" for w in ABSRET_WINDOWS] + [f"mkt_rv_{RV_WINDOW}m"]


def _minute_frame(closes: dict[str, list[float]]) -> pl.DataFrame:
    rows = []
    for symbol, series in closes.items():
        for i, close in enumerate(series):
            rows.append({"symbol": symbol, "minute": BASE + timedelta(minutes=i), "close": close})
    return pl.DataFrame(rows)


def _group() -> object:
    return REGISTRY.get_group("market_turbulence")


def _at(out: pl.DataFrame, minute_index: int, column: str) -> float | None:
    return out.filter(pl.col("minute") == BASE + timedelta(minutes=minute_index))[column].to_list()[0]


def test_registered_in_reduce_groups() -> None:
    """market_turbulence is a universe-wide GATHER → it MUST run in the reader's gather phase, not per
    shard (else each shard reduces ~1/8 of the universe and the live value can never match backfill)."""
    from quantlib.features.sharded_capture import REDUCE_GROUPS

    assert "market_turbulence" in REDUCE_GROUPS


def test_declares_expected_features() -> None:
    assert [spec.name for spec in _group().declare()] == FEATURE_NAMES


def test_absret_mean_matches_hand_computation() -> None:
    """mkt_absret_5m at T = universe equal-weight mean of |close[T]/close[T-5]-1|, nulls excluded."""
    closes = {
        "A": [100, 101, 102, 103, 104, 105.0],  # 5m return = 105/100-1 = +0.05 -> |.| = 0.05
        "B": [50, 50, 50, 50, 50, 49.0],  # 49/50-1 = -0.02 -> |.| = 0.02
        "C": [10, 10, 10, 10, 10, 10.0],  # 0.0
    }
    out = _group().compute(BatchContext(frames={"minute_agg": _minute_frame(closes)}))
    expected = (0.05 + 0.02 + 0.0) / 3
    assert _at(out, 5, "mkt_absret_5m") == pytest.approx(expected, rel=1e-12)


def test_null_lag_excluded_from_denominator() -> None:
    """At a minute with no bar exactly W ago, EVERY symbol's W-return is null → the universe mean is over
    an empty set → null (sparse), not a silently-shrunk denominator."""
    closes = {"A": [100, 101, 102, 103, 104, 105.0], "B": [50, 50, 50, 50, 50, 49.0]}
    out = _group().compute(BatchContext(frames={"minute_agg": _minute_frame(closes)}))
    # only 6 minutes exist, so the 30m/60m lags never resolve
    assert _at(out, 5, "mkt_absret_30m") is None
    assert _at(out, 5, "mkt_absret_60m") is None


def test_value_is_broadcast_across_universe() -> None:
    """The GATHER property: at any minute every symbol carries the IDENTICAL market-wide scalar."""
    closes = {
        sym: [100.0 * (1.0 + 0.001 * math.sin(i * 0.7 + s)) for i in range(65)]
        for s, sym in enumerate(["A", "B", "C", "D"])
    }
    out = _group().compute(BatchContext(frames={"minute_agg": _minute_frame(closes)}))
    last = out.filter(pl.col("minute") == out["minute"].max())
    assert last.height == 4
    for column in FEATURE_NAMES:
        assert last[column].n_unique() == 1, f"{column} not broadcast identically across the universe"


def test_realized_vol_matches_direct_std() -> None:
    """mkt_rv_30m at T = universe mean of each symbol's std of the 1m log returns over (T-30, T]."""
    # One symbol with a clean contiguous 1m grid so every step is a valid log return.
    rng = np.random.default_rng(3)
    px = 100.0
    series = [px]
    for _ in range(40):
        px *= float(np.exp(rng.normal(0, 0.002)))
        series.append(px)
    closes = {"A": series}
    out = _group().compute(BatchContext(frames={"minute_agg": _minute_frame(closes)}))
    t = 35  # well past the 30m warmup, full window of returns present
    logrets = np.diff(np.log(series[t - RV_WINDOW : t + 1]))  # the RV_WINDOW 1m returns ending at t
    expected = float(np.std(logrets, ddof=1))
    assert _at(out, t, f"mkt_rv_{RV_WINDOW}m") == pytest.approx(expected, rel=1e-9)


def test_realized_vol_requires_min_obs() -> None:
    """RV is undefined (null) until at least RV_MIN_OBS valid 1m returns are in the trailing window."""
    series = [100.0 + i for i in range(RV_MIN_OBS + 5)]
    out = _group().compute(BatchContext(frames={"minute_agg": _minute_frame({"A": series})}))
    # at minute RV_MIN_OBS-1 there are only RV_MIN_OBS-1 returns -> below the floor -> null
    assert _at(out, RV_MIN_OBS - 1, f"mkt_rv_{RV_WINDOW}m") is None
    # at minute RV_MIN_OBS there are exactly RV_MIN_OBS returns -> defined
    assert _at(out, RV_MIN_OBS, f"mkt_rv_{RV_WINDOW}m") is not None


def test_compute_latest_equals_compute_last() -> None:
    """The latest-minute gather must equal compute() filtered to the last minute, cell-for-cell to each
    feature's DECLARED parity tolerance. ``compute_latest`` overrides ``compute()`` for speed — it builds the
    per-symbol measures for the latest minute alone (a few ``close[T-W]`` lookups + one trailing-RV std
    slice) rather than the full-buffer rolling derive — so the universe-mean reduce may differ from the
    rolling form only by float-reassociation noise (~1e-19), well within tolerance. Null-vs-value mismatches
    (the parity break that matters) are still held exactly."""
    closes = {
        sym: [100.0 * (1.0 + 0.0013 * math.cos(i * 0.5 + s)) for i in range(65)]
        for s, sym in enumerate(["A", "B", "C", "D", "E"])
    }
    ctx = BatchContext(frames={"minute_agg": _minute_frame(closes)})
    group = _group()
    full = group.compute(ctx)
    last = full.filter(pl.col("minute") == full["minute"].max()).sort("symbol")
    latest = group.compute_latest(ctx).sort("symbol")
    assert latest.height == last.height
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    for column in FEATURE_NAMES:
        a = np.asarray(last[column].to_list(), dtype=float)
        b = np.asarray(latest[column].to_list(), dtype=float)
        null_mismatch = np.isnan(a) != np.isnan(b)
        assert not null_mismatch.any(), f"{column}: null-vs-value mismatch"
        both = ~(np.isnan(a) | np.isnan(b))
        bound = 1e-9 + tolerances[column] * np.abs(a)
        assert np.all(np.abs(a[both] - b[both]) <= bound[both]), f"{column}: beyond declared tolerance"


def _gappy_frame() -> pl.DataFrame:
    """A frame the fast ``compute_latest`` must survive: missing minutes (gaps), a non-positive close, a
    sparse late-entrant, and a symbol with NO bar at the latest minute — exactly the cases the time-based
    rolling form handles and the latest-minute fast path must reproduce."""
    rng = np.random.default_rng(7)
    n_minutes = 90
    rows = []
    for s in range(40):
        start = 0 if s < 30 else int(rng.integers(60, 80))  # sparse late entrants
        drop_final = s in (2, 5)  # these two have no bar at the latest minute
        price = 100.0
        for i in range(start, n_minutes):
            if drop_final and i >= n_minutes - 2:
                continue
            if rng.random() < 0.2:  # 20% missing minutes
                continue
            price *= 1.0 + rng.normal(0.0, 0.002)
            close = price if (s != 11 or i % 17 != 0) else 0.0  # occasional non-positive close
            rows.append({"symbol": f"S{s}", "minute": BASE + timedelta(minutes=i), "close": close})
    return pl.DataFrame(rows)


def test_compute_latest_matches_rolling_under_gaps() -> None:
    """The fast latest-minute path must equal ``compute()``'s last minute under gaps / sparse universe /
    non-positive closes / symbols-absent-at-T, to declared tolerance with NO null-vs-value mismatch. This is
    the regression guard for the latest-minute fast path's two hazards: a symbol with no bar at T must be
    excluded from the reduce, and the trailing-RV window boundary must match the rolling ``(T-W, T]``."""
    ctx = BatchContext(frames={"minute_agg": _gappy_frame()})
    group = _group()
    full = group.compute(ctx)
    latest_minute = full["minute"].max()
    last = full.filter(pl.col("minute") == latest_minute).sort("symbol")
    fast = group.compute_latest(ctx).sort("symbol").select(last.columns)
    assert fast.height == last.height, "fast path emitted a different symbol set than the rolling last minute"
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    for column in FEATURE_NAMES:
        merged = last.select("symbol", column).join(
            fast.select("symbol", pl.col(column).alias("_fast")), on="symbol"
        )
        bad = merged.filter(
            (pl.col(column).is_null() != pl.col("_fast").is_null())
            | (
                pl.col(column).is_not_null()
                & pl.col("_fast").is_not_null()
                & ((pl.col(column) - pl.col("_fast")).abs() > 1e-9 + tolerances[column] * pl.col(column).abs())
            )
        )
        assert bad.height == 0, f"{column}: fast latest != rolling.last on {bad.height} rows"


def test_universe_pin_bounds_denominator() -> None:
    """With a ``universe`` frame the reduce runs only over its members — a name outside the universe does
    NOT enter the turbulence mean even if it printed that minute (the breadth/market_context pin)."""
    closes = {
        "A": [100, 101, 102, 103, 104, 105.0],  # +0.05 over 5m
        "B": [50, 50, 50, 50, 50, 49.0],  # -0.02
        "OUTSIDER": [10, 10, 10, 10, 10, 13.0],  # +0.30, but not in the universe
    }
    frames = {
        "minute_agg": _minute_frame(closes),
        "universe": pl.DataFrame({"symbol": ["A", "B"]}),
    }
    out = _group().compute(BatchContext(frames=frames))
    expected = (0.05 + 0.02) / 2  # OUTSIDER excluded
    assert _at(out, 5, "mkt_absret_5m") == pytest.approx(expected, rel=1e-12)
