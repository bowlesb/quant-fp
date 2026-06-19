"""Unit tests for the dashboard liquidity-band reference surface (services/dashboard/liquidity_bands).

No live store: a tiny raw-bar parquet store is built in a tmp dir in the same
``/raw/bars/symbol=<S>/date=<D>/data.parquet`` layout the real tape uses, so the stage-1 reduction and
the point-in-time ADV-rank / band / stability maths are exercised end-to-end on a controlled fixture.
The pure helpers (band labels, the band-label expression, cuts parsing) are tested directly.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import liquidity_bands as lb  # noqa: E402  (path inserted above)


def _write_bar_day(root: Path, symbol: str, date_iso: str, close: float, volume: int) -> None:
    """Write a one-RTH-minute bar partition for (symbol, date) at the 09:30 ET (13:30 UTC) minute, so the
    stage-1 RTH reduction sees exactly ``close*volume`` dollar volume for that symbol-day."""
    part = root / "raw" / "bars" / f"symbol={symbol}" / f"date={date_iso}"
    part.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.fromisoformat(date_iso + "T13:30:00+00:00")
    pl.DataFrame(
        {
            "symbol": [symbol],
            "ts": [ts],
            "open": [close],
            "high": [close],
            "low": [close],
            "close": [close],
            "volume": [volume],
        }
    ).with_columns(pl.col("ts").dt.convert_time_zone("UTC")).write_parquet(part / "data.parquet")


def _build_store(root: Path, n_symbols: int, n_days: int) -> None:
    """A fixture store of ``n_symbols`` over ``n_days`` consecutive weekdays. Symbol k gets a constant
    dollar volume of (n_symbols - k) * 1e6, so symbol 0 is always the most liquid (rank 1) and symbol
    n_symbols-1 the least — a deterministic, fully-separated cross-section to assert ranks/bands against."""
    base = dt.date(2025, 1, 6)  # a Monday
    days: list[str] = []
    day = base
    while len(days) < n_days:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += dt.timedelta(days=1)
    for date_iso in days:
        for k in range(n_symbols):
            dollar_vol = (n_symbols - k) * 1_000_000.0
            _write_bar_day(root, f"S{k:03d}", date_iso, close=10.0, volume=int(dollar_vol / 10.0))


def test_band_labels_contiguous_cuts() -> None:
    assert lb.band_labels([500, 1000, 2000, 4000]) == [
        "1-500",
        "500-1000",
        "1000-2000",
        "2000-4000",
        "4000+",
    ]
    assert lb.band_labels([1000]) == ["1-1000", "1000+"]


def test_band_label_expr_maps_ranks_to_bands() -> None:
    ranks = pl.DataFrame({"adv_rank": [1, 500, 501, 1000, 1001, 4000, 4001, 9999]})
    out = ranks.with_columns(lb.band_label_expr([500, 1000, 2000, 4000]).alias("band"))
    assert out["band"].to_list() == [
        "1-500",
        "1-500",
        "500-1000",
        "500-1000",
        "1000-2000",
        "2000-4000",
        "4000+",
        "4000+",
    ]


def test_parse_cuts() -> None:
    assert lb.parse_cuts(None) is None
    assert lb.parse_cuts("  ") is None
    assert lb.parse_cuts("2000,500,1000") == [500, 1000, 2000]  # sorted ascending
    with pytest.raises(ValueError):
        lb.parse_cuts("500,-1")
    with pytest.raises(ValueError):
        lb.parse_cuts("500,500")  # not distinct
    with pytest.raises(ValueError):
        lb.parse_cuts("0")  # non-positive


def test_stage1_reduction_dollar_volume(tmp_path: Path) -> None:
    _build_store(tmp_path, n_symbols=3, n_days=1)
    daily = lb.build_daily_dollar_vol(str(tmp_path), force=True)
    assert daily.height == 3
    by_symbol = {row["symbol"]: row["rth_dollar_vol"] for row in daily.iter_rows(named=True)}
    # symbol k dollar vol = (3 - k) * 1e6
    assert by_symbol["S000"] == pytest.approx(3_000_000.0)
    assert by_symbol["S001"] == pytest.approx(2_000_000.0)
    assert by_symbol["S002"] == pytest.approx(1_000_000.0)


def test_adv_rank_is_point_in_time_and_warmed_up(tmp_path: Path) -> None:
    # Fewer than MIN_TRAILING_DAYS days -> no symbol is ranked yet (point-in-time warmup honored).
    _build_store(tmp_path, n_symbols=5, n_days=lb.MIN_TRAILING_DAYS - 1)
    daily = lb.build_daily_dollar_vol(str(tmp_path), force=True)
    ranked = lb.compute_adv_rank(daily, [2, 4])
    assert ranked.height == 0


def test_adv_rank_orders_by_dollar_volume(tmp_path: Path) -> None:
    n_days = lb.MIN_TRAILING_DAYS + 2
    _build_store(tmp_path, n_symbols=5, n_days=n_days)
    daily = lb.build_daily_dollar_vol(str(tmp_path), force=True)
    ranked = lb.compute_adv_rank(daily, [2, 4])  # bands: 1-2, 2-4, 4+
    last_date = sorted(ranked["date"].unique().to_list())[-1]
    snap = ranked.filter(pl.col("date") == last_date).sort("adv_rank")
    # S000 most liquid -> rank 1 band 1-2; S004 least -> rank 5 band 4+
    assert snap["symbol"].to_list() == ["S000", "S001", "S002", "S003", "S004"]
    assert snap["adv_rank"].to_list() == [1, 2, 3, 4, 5]
    assert snap["band"].to_list() == ["1-2", "1-2", "2-4", "2-4", "4+"]


def test_build_surface_snapshot_and_members(tmp_path: Path) -> None:
    n_days = lb.MIN_TRAILING_DAYS + 1
    _build_store(tmp_path, n_symbols=6, n_days=n_days)
    view = lb.build_liquidity_bands(str(tmp_path), cuts=[2, 4], days=0)
    assert view["n_ranked_symbols"] == 6
    assert view["band_labels"] == ["1-2", "2-4", "4+"]
    snap_bands = view["snapshot"]["bands"]
    assert snap_bands["1-2"]["n"] == 2
    assert snap_bands["2-4"]["n"] == 2
    assert snap_bands["4+"]["n"] == 2
    # the reproducible-universe export: band 2-4 holds the 3rd/4th most liquid names
    members = lb.band_members("2-4", str(tmp_path), cuts=[2, 4])
    assert members["n_members"] == 2
    assert [m["symbol"] for m in members["members"]] == ["S002", "S003"]


def test_stability_zero_when_ranks_constant(tmp_path: Path) -> None:
    # Constant per-symbol dollar volume -> ranks never change -> zero band crossings.
    _build_store(tmp_path, n_symbols=5, n_days=lb.MIN_TRAILING_DAYS + 3)
    view = lb.build_liquidity_bands(str(tmp_path), cuts=[2, 4], days=0)
    assert view["stability"]["overall_cross_rate"] == 0.0
    assert view["stability"]["n_transitions"] > 0  # there ARE day-pairs, just no crossings


def test_symbol_history_tracks_one_symbol(tmp_path: Path) -> None:
    n_days = lb.MIN_TRAILING_DAYS + 2
    _build_store(tmp_path, n_symbols=4, n_days=n_days)
    hist = lb.symbol_history("S000", str(tmp_path), cuts=[2])
    assert hist["n_dates"] == n_days - lb.MIN_TRAILING_DAYS + 1
    assert all(row["adv_rank"] == 1 for row in hist["history"])  # S000 always most liquid
    assert all(row["band"] == "1-2" for row in hist["history"])


def test_empty_store_returns_empty_surface(tmp_path: Path) -> None:
    view = lb.build_liquidity_bands(str(tmp_path), days=0)
    assert view["n_ranked_symbols"] == 0
    assert view["timeline"] == []
    assert view["snapshot"]["bands"] == {}
