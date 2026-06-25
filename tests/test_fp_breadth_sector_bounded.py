"""breadth / sector_return compute the latest-minute gather over a BOUNDED trailing window, not the whole
buffer. Both re-derived their windowed returns over the ENTIRE minute buffer (a per-symbol time-join per
window) then ran the cross-sectional reduce across all ~245 minutes, discarding all but the latest. Their
``compute_latest`` now uses ``compute_latest_on_window`` — the SAME ``compute`` on the buffer sliced to the
deepest return window — so the latest-minute output is byte-identical while the per-minute work drops from
O(buffer) to O(max-window).

This gate proves the bounded form == the whole-buffer form INCLUDING on a SPARSE (gappy) universe. Their
returns use the TIME-based ``lagged`` (as-of minute − w), so the minute-window slice keeps exactly the bars
each window reads — the sliced and whole-buffer reduces are identical cell-for-cell. The generic
``test_fp_latest`` already guards ``compute_latest == compute().last``; this adds the sparse-universe case the
dense profiler fixture hides, plus the explicit before/after equality.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl

from quantlib.features.base import BatchContext
from quantlib.features.groups.breadth import MINUTE_WINDOWS as BREADTH_WINDOWS
from quantlib.features.groups.breadth import BreadthGroup
from quantlib.features.groups.market_turbulence import ABSRET_WINDOWS as TURB_WINDOWS
from quantlib.features.groups.market_turbulence import MarketTurbulenceGroup
from quantlib.features.groups.sector_beta import WINDOWS as SECTOR_BETA_WINDOWS
from quantlib.features.groups.sector_beta import SectorBetaGroup
from quantlib.features.groups.sector_return import MINUTE_WINDOWS as SECTOR_WINDOWS
from quantlib.features.groups.sector_return import SectorReturnGroup

BASE = dt.datetime(2026, 6, 15, 13, 30, tzinfo=dt.timezone.utc)

GICS = ["Technology", "Financials", "Energy", "Health Care", "Industrials"]


def _sparse_minute_agg(
    n_sym: int, n_min: int, gap_period: int, gap_fraction: float, seed: int
) -> pl.DataFrame:
    """A gappy bar buffer: a ``gap_fraction`` slice of symbols is missing every ``gap_period``-th minute, so a
    time-based return at lag w hits an absent bar for them. Every symbol present at minute 0."""
    rng = np.random.default_rng(seed)
    symbols = [f"S{i:03d}" for i in range(n_sym)]
    gap_syms = set(symbols[: int(n_sym * gap_fraction)])
    minutes = [BASE + dt.timedelta(minutes=i) for i in range(n_min)]
    rows: list[dict[str, object]] = []
    for mi, minute in enumerate(minutes):
        gapped = mi > 0 and mi % gap_period == 0
        for si, symbol in enumerate(symbols):
            if gapped and symbol in gap_syms:
                continue
            price = 100.0 + si + mi * 0.1 + rng.standard_normal() * 0.05
            rows.append({"symbol": symbol, "minute": minute, "close": price})
    return (
        pl.DataFrame(rows)
        .with_columns(pl.col("minute").cast(pl.Datetime("us", "UTC")))
        .sort(["symbol", "minute"])
    )


def _daily(symbols: list[str], days: int) -> pl.DataFrame:
    rows = []
    for si, symbol in enumerate(symbols):
        for d in range(days):
            rows.append(
                {
                    "symbol": symbol,
                    "date": BASE.date() - dt.timedelta(days=days - 1 - d),
                    "close": 100.0 + si + d,
                }
            )
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date)).sort(["symbol", "date"])


def _reference(symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"symbol": symbols, "sector": [GICS[i % len(GICS)] for i in range(len(symbols))]})


def _assert_equal(old: pl.DataFrame, new: pl.DataFrame, label: str) -> None:
    cols = sorted(old.columns)
    old = old.sort("symbol").select(cols)
    new = new.sort("symbol").select(cols)
    assert old["symbol"].to_list() == new["symbol"].to_list(), f"{label}: symbol set differs"
    for col in cols:
        if col in ("symbol", "minute"):
            assert old[col].to_list() == new[col].to_list(), f"{label}.{col}"
            continue
        a = old[col].to_numpy().astype(np.float64)
        b = new[col].to_numpy().astype(np.float64)
        both_nan = np.isnan(a) & np.isnan(b)
        close = np.isclose(a, b, rtol=0.0, atol=1e-12, equal_nan=False)
        bad = ~(both_nan | close)
        assert (
            not bad.any()
        ), f"{label}.{col}: {int(bad.sum())} mismatches\n  whole={a[bad][:5]} bounded={b[bad][:5]}"


def _whole_buffer_latest(group, ctx: BatchContext) -> pl.DataFrame:
    out = group.compute(ctx)
    return out.filter(pl.col("minute") == out["minute"].max())


def test_breadth_bounded_equals_whole_buffer_sparse() -> None:
    stream = _sparse_minute_agg(n_sym=40, n_min=160, gap_period=7, gap_fraction=0.4, seed=11)
    symbols = sorted(stream["symbol"].unique().to_list())
    ctx = BatchContext(
        frames={"minute_agg": stream, "daily": _daily(symbols, 10), "reference": _reference(symbols)}
    )
    group = BreadthGroup()
    _assert_equal(_whole_buffer_latest(group, ctx), group.compute_latest(ctx), "breadth")
    assert max(BREADTH_WINDOWS) + 1 >= max(BREADTH_WINDOWS)


def test_sector_return_bounded_equals_whole_buffer_sparse() -> None:
    stream = _sparse_minute_agg(n_sym=40, n_min=160, gap_period=7, gap_fraction=0.4, seed=13)
    symbols = sorted(stream["symbol"].unique().to_list())
    ctx = BatchContext(frames={"minute_agg": stream, "reference": _reference(symbols)})
    group = SectorReturnGroup()
    _assert_equal(_whole_buffer_latest(group, ctx), group.compute_latest(ctx), "sector_return")
    assert max(SECTOR_WINDOWS) + 1 >= max(SECTOR_WINDOWS)


def test_sector_beta_bounded_equals_whole_buffer_sparse() -> None:
    stream = _sparse_minute_agg(n_sym=40, n_min=160, gap_period=7, gap_fraction=0.4, seed=17)
    symbols = sorted(stream["symbol"].unique().to_list())
    ctx = BatchContext(frames={"minute_agg": stream, "reference": _reference(symbols)})
    group = SectorBetaGroup()
    _assert_equal(_whole_buffer_latest(group, ctx), group.compute_latest(ctx), "sector_beta")
    assert max(SECTOR_BETA_WINDOWS) + 1 >= max(SECTOR_BETA_WINDOWS)


def test_market_turbulence_bounded_equals_whole_buffer_sparse() -> None:
    stream = _sparse_minute_agg(n_sym=40, n_min=160, gap_period=7, gap_fraction=0.4, seed=19)
    ctx = BatchContext(frames={"minute_agg": stream})
    group = MarketTurbulenceGroup()
    _assert_equal(_whole_buffer_latest(group, ctx), group.compute_latest(ctx), "market_turbulence")
    assert max(TURB_WINDOWS) + 1 >= max(TURB_WINDOWS)
