"""sector_return parity + math — the per-GICS-sector equal-weight return aggregate broadcast to each name.

Proves (a) the sector aggregate is the equal-weight mean trailing return of the symbol's OWN sector,
(b) the within-sector excess is own-return minus that aggregate, (c) an unmapped-sector name gets NULL
(no peer group), and (d) the live aggregate-at-T form equals the backfill rolling last minute
cell-for-cell.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features import REGISTRY
from quantlib.features.base import BatchContext
from quantlib.features.groups.sector_return import MINUTE_WINDOWS

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _minute_frame(closes: dict[str, list[float]], n_minutes: int) -> pl.DataFrame:
    rows = []
    for symbol, series in closes.items():
        for i in range(n_minutes):
            rows.append({"symbol": symbol, "minute": BASE + timedelta(minutes=i), "close": series[i]})
    return pl.DataFrame(rows)


def _reference(sectors: dict[str, str | None]) -> pl.DataFrame:
    return pl.DataFrame({"symbol": list(sectors.keys()), "sector": list(sectors.values())})


def _ctx(closes: dict[str, list[float]], sectors: dict[str, str | None], n_minutes: int) -> BatchContext:
    return BatchContext(
        frames={
            "minute_agg": _minute_frame(closes, n_minutes),
            "reference": _reference(sectors),
        }
    )


def test_sector_return_is_equal_weight_sector_mean() -> None:
    """Two names in 'technology', one in 'energy': the 5m sector_return of each tech name = the mean of
    the two tech names' 5m returns; the energy name = its own (sole-member) 5m return."""
    n_minutes = 12
    window = 5
    # constant per-minute multiplicative drift so the W-window return is exact and equal across minutes
    closes = {
        "AAA": [100.0 * (1.002**i) for i in range(n_minutes)],  # tech, +0.2%/min
        "BBB": [100.0 * (1.004**i) for i in range(n_minutes)],  # tech, +0.4%/min
        "EEE": [100.0 * (0.999**i) for i in range(n_minutes)],  # energy, -0.1%/min
    }
    sectors = {"AAA": "technology", "BBB": "technology", "EEE": "energy"}
    ctx = _ctx(closes, sectors, n_minutes)
    out = REGISTRY.get_group("sector_return").compute(ctx)
    last = out["minute"].max()
    row = lambda sym: out.filter((pl.col("symbol") == sym) & (pl.col("minute") == last)).row(0, named=True)

    ret_aaa = (1.002**window) - 1.0
    ret_bbb = (1.004**window) - 1.0
    ret_eee = (0.999**window) - 1.0
    tech_mean = (ret_aaa + ret_bbb) / 2.0

    assert abs(row("AAA")["sector_return_5m"] - tech_mean) < 1e-9
    assert abs(row("BBB")["sector_return_5m"] - tech_mean) < 1e-9
    assert abs(row("EEE")["sector_return_5m"] - ret_eee) < 1e-9
    # within-sector excess = own return minus the sector mean
    assert abs(row("AAA")["sector_excess_5m"] - (ret_aaa - tech_mean)) < 1e-9
    assert abs(row("BBB")["sector_excess_5m"] - (ret_bbb - tech_mean)) < 1e-9
    assert abs(row("EEE")["sector_excess_5m"] - 0.0) < 1e-9  # sole member: own == sector mean


def test_unknown_sector_is_null() -> None:
    """A name with a null/blank sector has no peer group → sector_return and sector_excess are NULL,
    not bucketed into an 'unknown' aggregate."""
    n_minutes = 12
    closes = {
        "AAA": [100.0 * (1.002**i) for i in range(n_minutes)],
        "ZZZ": [100.0 * (1.001**i) for i in range(n_minutes)],  # unmapped
        "YYY": [100.0 * (1.003**i) for i in range(n_minutes)],  # blank string
    }
    sectors: dict[str, str | None] = {"AAA": "technology", "ZZZ": None, "YYY": "  "}
    ctx = _ctx(closes, sectors, n_minutes)
    out = REGISTRY.get_group("sector_return").compute(ctx)
    last = out["minute"].max()
    for sym in ("ZZZ", "YYY"):
        row = out.filter((pl.col("symbol") == sym) & (pl.col("minute") == last)).row(0, named=True)
        for window in MINUTE_WINDOWS:
            assert row[f"sector_return_{window}m"] is None, f"{sym} sector_return_{window}m should be NULL"
            assert row[f"sector_excess_{window}m"] is None, f"{sym} sector_excess_{window}m should be NULL"
    # the mapped name is unaffected: its (sole-member) tech aggregate equals its own return
    aaa = out.filter((pl.col("symbol") == "AAA") & (pl.col("minute") == last)).row(0, named=True)
    assert aaa["sector_return_5m"] is not None


def test_sector_return_latest_matches_rolling() -> None:
    """Live aggregate-at-T == backfill rolling last minute, cell-for-cell, on a multi-sector stream."""
    n_minutes = 90
    sector_names = ("technology", "healthcare", "energy", None)  # include an unmapped name
    closes: dict[str, list[float]] = {}
    sectors: dict[str, str | None] = {}
    for idx in range(16):
        sym = f"S{idx}"
        price = 100.0 + idx
        series = []
        for i in range(n_minutes):
            price *= 1.0 + ((idx - 8) * 0.0006) + (0.0004 if (i + idx) % 3 else -0.0005)
            series.append(price)
        closes[sym] = series
        sectors[sym] = sector_names[idx % len(sector_names)]
    ctx = _ctx(closes, sectors, n_minutes)

    group = REGISTRY.get_group("sector_return")
    rolling = group.compute(ctx)
    last = rolling["minute"].max()
    expected = rolling.filter(pl.col("minute") == last).sort("symbol")
    actual = (
        group.compute_latest(ctx).filter(pl.col("minute") == last).sort("symbol").select(expected.columns)
    )
    assert actual.height == expected.height
    for feature in [c for c in expected.columns if c not in ("symbol", "minute")]:
        joined = expected.select("symbol", feature).join(
            actual.select("symbol", pl.col(feature).alias("_a")), on="symbol"
        )
        bad = joined.filter(
            ~(
                (pl.col(feature).is_null() & pl.col("_a").is_null())
                | ((pl.col(feature) - pl.col("_a")).abs() <= 1e-12)
            )
        )
        assert (
            bad.height == 0
        ), f"sector_return.{feature}: compute_latest != compute().last on {bad.height} rows"
    # the aggregate genuinely discriminates across sectors (not all equal)
    vals = expected["sector_return_5m"].drop_nulls().to_list()
    assert vals and min(vals) < max(vals)
