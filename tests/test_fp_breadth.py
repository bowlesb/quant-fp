"""Breadth parity — an aggregate of a DISCONTINUOUS function, made parity-true by the dead-band.

Breadth counts ``sign(return)``, and sign jumps at 0: a return that differs by less than a cell
tolerance between two sources (legit float/tick-order noise) can flip a symbol across zero and change
the integer count, so cell tolerance does NOT compose into the aggregate. These tests prove (a) the
live aggregate-at-T form equals the backfill rolling form cell-for-cell, INCLUDING a stream where
several names sit right on the zero boundary, and (b) the dead-band makes the count robust to sub-EPS
return perturbations that would otherwise flip a sign and move the breadth value.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

import pytest

from quantlib.features import REGISTRY
from quantlib.features.base import BatchContext
from quantlib.features.groups.breadth import EPS, MINUTE_WINDOWS

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
SECTORS = ("technology", "healthcare", "energy")


def _minute_frame(symbols: list[str], n_minutes: int, closes: dict[str, list[float]]) -> pl.DataFrame:
    rows = []
    for symbol in symbols:
        for i in range(n_minutes):
            rows.append({"symbol": symbol, "minute": BASE + timedelta(minutes=i), "close": closes[symbol][i]})
    return pl.DataFrame(rows)


def _reference(symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": symbols, "sector": [SECTORS[idx % len(SECTORS)] for idx in range(len(symbols))]}
    )


def _daily(symbols: list[str], n_days: int = 30) -> pl.DataFrame:
    rows = []
    for offset, symbol in enumerate(symbols):
        for day in range(n_days):
            rows.append(
                {
                    "symbol": symbol,
                    "date": (BASE + timedelta(days=day - n_days + 1)).date(),
                    "close": 100.0 + offset + day * 0.5,
                }
            )
    return pl.DataFrame(rows)


def _ctx(symbols: list[str], n_minutes: int, closes: dict[str, list[float]]) -> BatchContext:
    return BatchContext(
        frames={
            "minute_agg": _minute_frame(symbols, n_minutes, closes),
            "reference": _reference(symbols),
            "daily": _daily(symbols),
        }
    )


def _assert_latest_matches_rolling(ctx: BatchContext) -> pl.DataFrame:
    group = REGISTRY.get_group("breadth")
    rolling = group.compute(ctx)
    latest = rolling["minute"].max()
    expected = rolling.filter(pl.col("minute") == latest).sort("symbol")
    actual = group.compute_latest(ctx).filter(pl.col("minute") == latest).sort("symbol").select(expected.columns)
    assert actual.height == expected.height
    tolerances = {spec.name: spec.tolerance for spec in group.declare()}
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
        assert bad.height == 0, f"breadth.{feature}: compute_latest != compute().last on {bad.height} rows"
    return expected


def test_breadth_latest_matches_rolling_general() -> None:
    """Live aggregate-at-T == backfill rolling last minute, on a varied-return stream (non-degenerate
    breadth: a mix of up/down/flat names across sectors)."""
    symbols = [f"S{i}" for i in range(12)]
    n_minutes = 80
    closes: dict[str, list[float]] = {}
    for offset, symbol in enumerate(symbols):
        series = []
        price = 100.0 + offset
        for i in range(n_minutes):
            # deterministic up/down drift differing by symbol so breadth is a real mix
            price *= 1.0 + ((offset - 6) * 0.0008) + (0.0005 if (i + offset) % 3 else -0.0006)
            series.append(price)
        closes[symbol] = series
    ctx = _ctx(symbols, n_minutes, closes)
    expected = _assert_latest_matches_rolling(ctx)
    # breadth must be a genuine fraction in [0, 1], not degenerate
    up = expected["breadth_up_5m"].drop_nulls().to_list()
    assert up, "no breadth values produced"
    assert all(0.0 <= value <= 1.0 for value in up)
    assert min(up) < max(up) or 0.0 < up[0] < 1.0  # some discrimination across the universe


def test_breadth_boundary_case_is_robust() -> None:
    """THE PARITY POINT. Several names end the window with a return sitting RIGHT on the zero boundary
    (|ret| < EPS). The dead-band must (a) keep compute_latest == compute cell-for-cell, and (b) classify
    those names FLAT (in the denominator, neither up nor down), so a sub-EPS perturbation cannot move the
    breadth count. We prove (b) by perturbing the boundary names by < EPS and asserting breadth is
    UNCHANGED — the exact noise that would flip a naive sign(return)>0 count."""
    window = MINUTE_WINDOWS[0]  # 5m
    n_minutes = 20
    boundary = [f"B{i}" for i in range(5)]  # will sit inside the dead-band
    movers_up = [f"U{i}" for i in range(4)]
    movers_down = [f"D{i}" for i in range(3)]
    symbols = boundary + movers_up + movers_down

    def _series(start: float, ret_over_window: float) -> list[float]:
        # flat until the window-start, then end so that close[T]/close[T-window]-1 == ret_over_window
        prices = [start] * n_minutes
        prices[-1] = start * (1.0 + ret_over_window)
        return prices

    # boundary names: tiny returns INSIDE the dead-band (half of +EPS), and exactly +EPS/2, -EPS/2
    tiny = [EPS * 0.5, -EPS * 0.5, EPS * 0.25, -EPS * 0.25, 0.0]
    closes: dict[str, list[float]] = {}
    for idx, symbol in enumerate(boundary):
        closes[symbol] = _series(100.0 + idx, tiny[idx])
    for idx, symbol in enumerate(movers_up):
        closes[symbol] = _series(50.0 + idx, 0.01)  # clearly up
    for idx, symbol in enumerate(movers_down):
        closes[symbol] = _series(80.0 + idx, -0.01)  # clearly down

    ctx = _ctx(symbols, n_minutes, closes)
    expected = _assert_latest_matches_rolling(ctx)

    # market breadth at the latest minute: 9 names have a valid 5m return (4 up, 3 down, 5 flat... wait
    # boundary has 5). denominator = 12 valid names; up = 4/12, down = 3/12.
    feat = f"breadth_up_{window}m"
    up_frac = expected[feat].drop_nulls().unique().to_list()
    down_frac = expected[f"breadth_down_{window}m"].drop_nulls().unique().to_list()
    assert len(up_frac) == 1 and len(down_frac) == 1, "market breadth must be one scalar broadcast to all"
    assert up_frac[0] == pytest.approx(4.0 / 12.0)  # only the clear movers count up
    assert down_frac[0] == pytest.approx(3.0 / 12.0)  # boundary names are FLAT, not counted

    # ROBUSTNESS: perturb the boundary names by < EPS (the noise that would flip a naive sign) and assert
    # breadth is IDENTICAL — this is what the dead-band buys.
    perturbed = {symbol: list(series) for symbol, series in closes.items()}
    for idx, symbol in enumerate(boundary):
        # nudge each boundary close by a sub-EPS amount (could flip it across zero under a naive sign)
        nudge = EPS * 0.4 * (1.0 if idx % 2 else -1.0)
        start = perturbed[symbol][0]
        perturbed[symbol][-1] = start * (1.0 + tiny[idx] + nudge)
    ctx2 = _ctx(symbols, n_minutes, perturbed)
    group = REGISTRY.get_group("breadth")
    rolling2 = group.compute(ctx2)
    latest2 = rolling2["minute"].max()
    after = rolling2.filter(pl.col("minute") == latest2)
    assert after[feat].drop_nulls().unique().to_list()[0] == pytest.approx(4.0 / 12.0)
    assert after[f"breadth_down_{window}m"].drop_nulls().unique().to_list()[0] == pytest.approx(3.0 / 12.0)


def test_sector_breadth_joins_by_sector() -> None:
    """Sector breadth is the same reduce grouped BY sector, joined onto each ticker by ITS sector — so
    two tickers in the same sector carry the same sector breadth, and it can differ across sectors."""
    # one sector all-up, one sector all-down -> sector breadth differs by sector
    symbols = ["TECH1", "TECH2", "ENER1", "ENER2"]
    n_minutes = 12
    ref = pl.DataFrame(
        {"symbol": symbols, "sector": ["technology", "technology", "energy", "energy"]}
    )

    def _series(start: float, ret: float) -> list[float]:
        prices = [start] * n_minutes
        prices[-1] = start * (1.0 + ret)
        return prices

    closes = {
        "TECH1": _series(100.0, 0.02), "TECH2": _series(101.0, 0.02),  # both up
        "ENER1": _series(50.0, -0.02), "ENER2": _series(51.0, -0.02),  # both down
    }
    ctx = BatchContext(
        frames={
            "minute_agg": _minute_frame(symbols, n_minutes, closes),
            "reference": ref,
            "daily": _daily(symbols),
        }
    )
    group = REGISTRY.get_group("breadth")
    out = group.compute(ctx)
    latest = out["minute"].max()
    last = out.filter(pl.col("minute") == latest)
    by_symbol = {row["symbol"]: row for row in last.iter_rows(named=True)}
    assert by_symbol["TECH1"]["sector_breadth_up_5m"] == pytest.approx(1.0)
    assert by_symbol["TECH1"]["sector_breadth_up_5m"] == by_symbol["TECH2"]["sector_breadth_up_5m"]
    assert by_symbol["ENER1"]["sector_breadth_up_5m"] == pytest.approx(0.0)
    assert by_symbol["ENER1"]["sector_breadth_down_5m"] == pytest.approx(1.0)
    # market breadth is the SAME scalar for everyone (2 up, 2 down of 4)
    assert by_symbol["TECH1"]["breadth_up_5m"] == pytest.approx(0.5)
    assert by_symbol["ENER1"]["breadth_up_5m"] == pytest.approx(0.5)


def test_null_sector_buckets_to_unknown() -> None:
    """A null-sector ticker is never dropped — it buckets to UNKNOWN and gets that bucket's breadth."""
    symbols = ["A", "B"]
    n_minutes = 12
    ref = pl.DataFrame({"symbol": symbols, "sector": [None, None]}, schema={"symbol": pl.String, "sector": pl.String})

    def _series(start: float, ret: float) -> list[float]:
        prices = [start] * n_minutes
        prices[-1] = start * (1.0 + ret)
        return prices

    closes = {"A": _series(100.0, 0.02), "B": _series(50.0, -0.02)}
    ctx = BatchContext(
        frames={
            "minute_agg": _minute_frame(symbols, n_minutes, closes),
            "reference": ref,
            "daily": _daily(symbols),
        }
    )
    group = REGISTRY.get_group("breadth")
    out = group.compute(ctx)
    latest = out["minute"].max()
    last = out.filter(pl.col("minute") == latest)
    # both names are in the same UNKNOWN sector: 1 up, 1 down -> sector breadth up = 0.5 for each
    for row in last.iter_rows(named=True):
        assert row["sector_breadth_up_5m"] == pytest.approx(0.5)
        assert row["sector_breadth_down_5m"] == pytest.approx(0.5)
