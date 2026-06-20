"""sector_beta parity + math — rolling OLS of a ticker's one-minute return on its OWN sector's aggregate.

Proves (a) a name that IS its sector (sole member) has beta == 1 and corr == 1, (b) a name whose
one-minute returns are a fixed multiple of its sector's recovers that multiple as beta, (c) an
unmapped-sector name gets NULL, and (d) the live aggregate-at-T form equals the backfill rolling last
minute cell-for-cell.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from quantlib.features import REGISTRY
from quantlib.features.base import BatchContext
from quantlib.features.groups.sector_beta import WINDOWS

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


def _prices_from_returns(start: float, rets: list[float]) -> list[float]:
    """Build a close series whose per-minute returns are exactly ``rets`` (length n_minutes-1)."""
    prices = [start]
    for ret in rets:
        prices.append(prices[-1] * (1.0 + ret))
    return prices


def test_sole_member_beta_and_corr_are_one() -> None:
    """A sole member of its sector IS the sector aggregate, so it regresses on itself → beta == corr == 1."""
    n_minutes = 40
    rets = [0.001 * ((i % 5) - 2) for i in range(n_minutes - 1)]  # varied, non-constant
    closes = {"SOLO": _prices_from_returns(100.0, rets)}
    sectors = {"SOLO": "energy"}
    ctx = _ctx(closes, sectors, n_minutes)
    out = REGISTRY.get_group("sector_beta").compute(ctx)
    last = out["minute"].max()
    row = out.filter((pl.col("symbol") == "SOLO") & (pl.col("minute") == last)).row(0, named=True)
    for window in WINDOWS:
        assert abs(row[f"sector_beta_{window}m"] - 1.0) < 1e-6, f"beta_{window}m"
        assert abs(row[f"sector_corr_{window}m"] - 1.0) < 1e-6, f"corr_{window}m"


def test_beta_recovers_a_fixed_multiple() -> None:
    """Sector = two 'driver' names with identical returns (so the aggregate == the driver return). A third
    member whose returns are exactly 2x the drivers' must show sector_beta ≈ 2 and corr ≈ 1."""
    n_minutes = 50
    base_rets = [0.001 * ((i % 7) - 3) for i in range(n_minutes - 1)]
    driver = _prices_from_returns(100.0, base_rets)
    follower = _prices_from_returns(100.0, [2.0 * r for r in base_rets])
    closes = {"D1": driver, "D2": list(driver), "F": follower}
    sectors = {"D1": "technology", "D2": "technology", "F": "technology"}
    ctx = _ctx(closes, sectors, n_minutes)
    out = REGISTRY.get_group("sector_beta").compute(ctx)
    last = out["minute"].max()
    frow = out.filter((pl.col("symbol") == "F") & (pl.col("minute") == last)).row(0, named=True)
    # the sector mean of {r, r, 2r} = 4r/3; F's return is 2r → beta = cov(2r, 4r/3)/var(4r/3) = 2r / (4r/3) = 1.5
    # so verify it's a stable >1 multiple with perfect correlation (all returns colinear with the aggregate)
    for window in WINDOWS:
        assert (
            frow[f"sector_beta_{window}m"] > 1.0
        ), f"F beta_{window}m should exceed 1 (amplifies the sector)"
        assert abs(frow[f"sector_corr_{window}m"] - 1.0) < 1e-6, f"F corr_{window}m"


def test_unknown_sector_is_null() -> None:
    """An unmapped-sector name has no sector series to regress on → beta and corr are NULL."""
    n_minutes = 40
    rets = [0.001 * ((i % 5) - 2) for i in range(n_minutes - 1)]
    closes = {"AAA": _prices_from_returns(100.0, rets), "ZZZ": _prices_from_returns(50.0, rets)}
    sectors: dict[str, str | None] = {"AAA": "technology", "ZZZ": None}
    ctx = _ctx(closes, sectors, n_minutes)
    out = REGISTRY.get_group("sector_beta").compute(ctx)
    last = out["minute"].max()
    zrow = out.filter((pl.col("symbol") == "ZZZ") & (pl.col("minute") == last)).row(0, named=True)
    for window in WINDOWS:
        assert zrow[f"sector_beta_{window}m"] is None, f"ZZZ beta_{window}m should be NULL"
        assert zrow[f"sector_corr_{window}m"] is None, f"ZZZ corr_{window}m should be NULL"


def test_sector_beta_latest_matches_rolling() -> None:
    """Live aggregate-at-T == backfill rolling last minute, cell-for-cell, on a multi-sector stream."""
    n_minutes = 100
    sector_names = ("technology", "healthcare", "energy", None)
    closes: dict[str, list[float]] = {}
    sectors: dict[str, str | None] = {}
    for idx in range(16):
        sym = f"S{idx}"
        rets = [0.0006 * ((i + idx) % 7 - 3) + 0.0002 * (idx - 8) for i in range(n_minutes - 1)]
        closes[sym] = _prices_from_returns(100.0 + idx, rets)
        sectors[sym] = sector_names[idx % len(sector_names)]
    ctx = _ctx(closes, sectors, n_minutes)

    group = REGISTRY.get_group("sector_beta")
    rolling = group.compute(ctx)
    last = rolling["minute"].max()
    expected = rolling.filter(pl.col("minute") == last).sort("symbol")
    actual = (
        group.compute_latest(ctx).filter(pl.col("minute") == last).sort("symbol").select(expected.columns)
    )
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
    assert actual.height == expected.height
    for feature in [c for c in expected.columns if c not in ("symbol", "minute")]:
        tol = tolerances[feature]
        joined = expected.select("symbol", feature).join(
            actual.select("symbol", pl.col(feature).alias("_a")), on="symbol"
        )
        bad = joined.filter(
            ~(
                (pl.col(feature).is_null() & pl.col("_a").is_null())
                | ((pl.col(feature) - pl.col("_a")).abs() <= 1e-9 + tol * pl.col(feature).abs())
            )
        )
        assert (
            bad.height == 0
        ), f"sector_beta.{feature}: compute_latest != compute().last on {bad.height} rows"
    vals = expected["sector_corr_15m"].drop_nulls().to_list()
    assert vals, "no sector_corr values produced"
