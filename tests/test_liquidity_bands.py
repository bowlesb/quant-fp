"""Unit tests for the canonical ADV-rank / liquidity-band surface (services/dashboard/liquidity_bands).

No live store: tiny raw-bar partitions are written into a tmp ``<store>/raw/bars/symbol=X/date=Y/`` tree so
the full pipeline (daily RTH dollar-vol reduction -> trailing-20d ADV -> stable rank -> canonical bands ->
composition + stability) is exercised end-to-end against a controlled fixture with a KNOWN ADV ordering. The
band/rank maths and the symbol-lookup / band-members views are asserted directly.

The band-rank constants are shrunk to tiny values via monkeypatch so a handful of fixture symbols populate
distinct bands without needing thousands of rows. The RTH window, ADV window, and rank-floor logic are the
real ones.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import liquidity_bands as lb  # noqa: E402  (path inserted above)

_RTH_TS = dt.datetime(2026, 6, 1, 14, 0, tzinfo=dt.timezone.utc)  # 14:00 UTC = inside RTH [1330, 1959]
_PRE_TS = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.timezone.utc)  # 12:00 UTC = pre-market, must be excluded


def _write_bars(store: Path, symbol: str, date_iso: str, minute_bars: list[tuple[float, int]]) -> None:
    """Write one symbol-date raw-bar partition. ``minute_bars`` is a list of (close, volume) at RTH minutes;
    one extra pre-market bar is always added to prove the RTH filter drops it."""
    part = store / "raw" / "bars" / f"symbol={symbol}" / f"date={date_iso}"
    part.mkdir(parents=True, exist_ok=True)
    closes = [close for close, _ in minute_bars] + [999.0]  # the 999 is the pre-market bar (excluded)
    volumes = [vol for _, vol in minute_bars] + [10_000]
    times = [_RTH_TS] * len(minute_bars) + [_PRE_TS]
    pl.DataFrame(
        {
            "symbol": [symbol] * len(closes),
            "ts": times,
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": volumes,
            "vwap": closes,
            "trade_count": [1] * len(closes),
        }
    ).write_parquet(part / "data.parquet")


def _build_store(tmp_path: Path, dollar_vol_by_symbol: dict[str, float], n_dates: int) -> Path:
    """A store where each symbol has a CONSTANT daily RTH dollar volume across ``n_dates`` consecutive dates
    (so its trailing-20d ADV == that value, and the cross-sectional rank is unambiguous)."""
    store = tmp_path / "store"
    base = dt.date(2026, 3, 2)  # a Monday; we just need distinct sorted ISO dates
    for offset in range(n_dates):
        date_iso = (base + dt.timedelta(days=offset)).isoformat()
        for symbol, dollar_vol in dollar_vol_by_symbol.items():
            # one RTH minute bar: close=10, volume chosen so close*volume == dollar_vol
            volume = int(round(dollar_vol / 10.0))
            _write_bars(store, symbol, date_iso, [(10.0, volume)])
    return store


@pytest.fixture()
def tiny_bands(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the band rank-ranges + rank floor so a few fixture symbols populate distinct bands."""
    monkeypatch.setattr(
        lb,
        "BANDS",
        [
            ("B1", "top", 1, 3),  # ranks 1-2
            ("B2", "mid", 3, 5),  # ranks 3-4
            ("B3", "low", 5, 7),  # ranks 5-6
        ],
    )
    monkeypatch.setattr(lb, "MIN_DAYS_FOR_RANK", 3)
    monkeypatch.setattr(lb, "ADV_WINDOW", 2)
    # the fixtures are short, so use a 2-day stability lookback that fits the window
    monkeypatch.setattr(lb, "STABILITY_LOOKBACKS", [2])


def test_daily_dollar_vol_sums_rth_only(tmp_path: Path) -> None:
    store = _build_store(tmp_path, {"AAA": 1_000_000.0}, n_dates=1)
    date_iso = lb._store_dates(str(store))[0]
    daily = lb._daily_dollar_vol(str(store), date_iso)
    assert daily.height == 1
    assert daily["symbol"].to_list() == ["AAA"]
    # the 999*10000 pre-market bar must be EXCLUDED -> exactly the RTH dollar volume, not + 9.99M
    assert daily["rth_dollar_vol"][0] == pytest.approx(1_000_000.0, rel=1e-6)


def test_band_assignment_follows_adv_rank(tmp_path: Path, tiny_bands: None) -> None:
    # six symbols, strictly decreasing ADV -> ranks 1..6 -> B1{1,2} B2{3,4} B3{5,6}
    advs = {"S1": 6e9, "S2": 5e9, "S3": 4e9, "S4": 3e9, "S5": 2e9, "S6": 1e9}
    store = _build_store(tmp_path, advs, n_dates=4)
    surface = lb.build_liquidity_bands(str(store), window_days=10)

    assert surface["n_ranked_symbols"] == 6
    assert surface["anchor_date"] == lb._store_dates(str(store))[-1]
    bands = {b["band"]: b for b in surface["bands"]}
    assert bands["B1"]["n_symbols"] == 2
    assert bands["B2"]["n_symbols"] == 2
    assert bands["B3"]["n_symbols"] == 2
    # B1 holds the two most-liquid (6e9, 5e9): median 5.5e9, min 5e9, max 6e9
    assert bands["B1"]["adv_max"] == pytest.approx(6e9)
    assert bands["B1"]["adv_min"] == pytest.approx(5e9)
    assert bands["B1"]["adv_median"] == pytest.approx(5.5e9)
    assert bands["B1"]["rank_lo"] == 1 and bands["B1"]["rank_hi"] == 2


def test_rank_floor_excludes_short_history(tmp_path: Path, tiny_bands: None) -> None:
    # AAA has 5 dates (>=3 floor -> ranked); SHORT has 2 dates (< floor -> NOT ranked).
    store = tmp_path / "store"
    base = dt.date(2026, 3, 2)
    for offset in range(5):
        date_iso = (base + dt.timedelta(days=offset)).isoformat()
        _write_bars(store, "AAA", date_iso, [(10.0, 100_000)])
        if offset < 2:
            _write_bars(store, "SHORT", date_iso, [(10.0, 999_999)])
    surface = lb.build_liquidity_bands(str(store), window_days=10)
    # SHORT has the larger dollar-vol but too few days -> excluded from the ranked universe.
    assert surface["n_ranked_symbols"] == 1


def test_lookup_symbol_found_and_missing(tmp_path: Path, tiny_bands: None) -> None:
    advs = {"S1": 6e9, "S2": 5e9, "S3": 4e9, "S4": 3e9, "S5": 2e9, "S6": 1e9}
    store = _build_store(tmp_path, advs, n_dates=4)
    hit = lb.lookup_symbol("s1", str(store), window_days=10)  # lower-case -> upper-cased
    assert hit["found"] is True
    assert hit["symbol"] == "S1"
    assert hit["rank"] == 1
    assert hit["band"] == "B1"
    assert hit["adv"] == pytest.approx(6e9)
    miss = lb.lookup_symbol("NOPE", str(store), window_days=10)
    assert miss == {"symbol": "NOPE", "found": False}


def test_band_members_ordered_by_rank(tmp_path: Path, tiny_bands: None) -> None:
    advs = {"S1": 6e9, "S2": 5e9, "S3": 4e9, "S4": 3e9, "S5": 2e9, "S6": 1e9}
    store = _build_store(tmp_path, advs, n_dates=4)
    members = lb.band_members("B2", str(store), window_days=10)
    assert members["band"] == "B2"
    assert members["n_symbols"] == 2
    # B2 = ranks 3,4 = S3 then S4 (most liquid first)
    assert [m["symbol"] for m in members["members"]] == ["S3", "S4"]
    assert members["members"][0]["rank"] == 3


def test_band_members_limit(tmp_path: Path, tiny_bands: None) -> None:
    advs = {"S1": 6e9, "S2": 5e9, "S3": 4e9, "S4": 3e9, "S5": 2e9, "S6": 1e9}
    store = _build_store(tmp_path, advs, n_dates=4)
    members = lb.band_members("B1", str(store), window_days=10, limit=1)
    assert members["n_symbols"] == 2  # the true count
    assert members["shown"] == 1  # but only 1 returned
    assert len(members["members"]) == 1


def test_empty_store_returns_zeroed_surface(tmp_path: Path) -> None:
    store = tmp_path / "store"
    (store / "raw" / "bars").mkdir(parents=True)
    surface = lb.build_liquidity_bands(str(store), window_days=10)
    assert surface["anchor_date"] is None
    assert surface["n_ranked_symbols"] == 0
    # every canonical band is still listed (zeroed) so the page renders a stable shape.
    assert [b["band"] for b in surface["bands"]] == [name for name, *_ in lb.BANDS]
    assert all(b["n_symbols"] == 0 and b["adv_median"] is None for b in surface["bands"])


def test_stability_point_in_time_full_retention_when_static(tmp_path: Path, tiny_bands: None) -> None:
    # ADV is constant per symbol across all dates -> point-in-time bands never change -> 100% retention.
    advs = {"S1": 6e9, "S2": 5e9, "S3": 4e9, "S4": 3e9, "S5": 2e9, "S6": 1e9}
    store = _build_store(tmp_path, advs, n_dates=10)
    surface = lb.build_liquidity_bands(str(store), window_days=20)
    stability = {s["band"]: s for s in surface["stability"]}
    assert stability["B1"]["retained_2d_pct"] == 100.0


def test_cache_reuses_within_ttl(tmp_path: Path, tiny_bands: None) -> None:
    advs = {"S1": 6e9, "S2": 5e9, "S3": 4e9, "S4": 3e9, "S5": 2e9, "S6": 1e9}
    store = _build_store(tmp_path, advs, n_dates=4)
    cache = lb.LiquidityBandsCache(ttl=600.0)
    first = cache.surface(str(store), window_days=10)
    second = cache.surface(str(store), window_days=10)
    assert first is second  # same cached object, not rebuilt
    # the lookup view reuses the cached ranked table
    assert cache.lookup("S1", str(store), window_days=10)["rank"] == 1
    assert cache.members("B1", str(store), window_days=10)["n_symbols"] == 2
