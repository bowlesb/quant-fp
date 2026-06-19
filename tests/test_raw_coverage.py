"""Unit tests for the dashboard RAW-TAPE coverage aggregation (services/dashboard/raw_coverage).

No live store: a tiny manifest is written into a tmp ``<store>/raw/_manifest_<tier>.d/`` part so the JSON the
``/api/raw-coverage`` endpoint serves is exercised end-to-end (build_raw_coverage) against a controlled
fixture. The pure helpers (real-cell filter, per-tier coverage, recent-window clip) are tested directly.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl

from quantlib.data.raw_store import MANIFEST_SCHEMA

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import raw_coverage as rc  # noqa: E402  (path inserted above)


def _write_manifest(store: Path, tier: str, rows: list[dict]) -> None:
    parts_dir = store / "raw" / f"_manifest_{tier}.d"
    parts_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        [
            {
                "tier": tier,
                "symbol": row["symbol"],
                "date": row["date"],
                "rows": row["rows"],
                "bytes": 100,
                "fetched_at": dt.datetime(2026, 6, 18, tzinfo=dt.timezone.utc),
            }
            for row in rows
        ],
        schema=MANIFEST_SCHEMA,
    ).write_parquet(parts_dir / "part-1-00000001.parquet")


def test_real_cells_drops_zero_rows_and_takes_max() -> None:
    manifest = pl.DataFrame(
        {
            "symbol": ["AAA", "AAA", "BBB", "CCC"],
            "date": ["2026-06-15", "2026-06-15", "2026-06-15", "2026-06-15"],
            "rows": [0, 380, 0, 0],  # AAA poisoned-then-real; BBB/CCC settled-empty
        }
    )
    real = rc._real_cells(manifest)
    assert real.height == 1
    assert real["symbol"].to_list() == ["AAA"]
    assert real["rows"].to_list() == [380]


def test_tier_coverage_depth_and_breadth(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "bars",
        [
            {"symbol": "AAA", "date": "2026-06-15", "rows": 380},
            {"symbol": "BBB", "date": "2026-06-15", "rows": 200},
            {"symbol": "AAA", "date": "2026-06-17", "rows": 380},
            {"symbol": "ZZZ", "date": "2026-06-16", "rows": 0},  # no real tape -> excluded
        ],
    )
    cov = rc._tier_coverage(str(tmp_path), "bars")
    assert cov["earliest"] == "2026-06-15"
    assert cov["latest"] == "2026-06-17"
    assert cov["span_days"] == 3  # 15,16,17 inclusive
    assert cov["n_dates"] == 2  # only 15 and 17 have a real tape (16 was 0-row)
    assert cov["n_symbols"] == 2  # AAA, BBB
    assert cov["newest_symbols_per_day"] == 1  # 06-17 has only AAA
    timeline = cov["dates"]
    assert [cell["date"] for cell in timeline] == ["2026-06-15", "2026-06-17"]
    assert timeline[0]["n_symbols"] == 2


def test_tier_coverage_empty_when_no_manifest(tmp_path: Path) -> None:
    cov = rc._tier_coverage(str(tmp_path), "quotes")
    assert cov["earliest"] is None
    assert cov["n_dates"] == 0
    assert cov["dates"] == []


def test_clip_recent_trims_timeline_but_keeps_full_depth() -> None:
    tier = {
        "tier": "bars",
        "earliest": "2026-06-01",
        "latest": "2026-06-10",
        "span_days": 10,
        "n_dates": 3,
        "dates": [
            {"date": "2026-06-01", "n_symbols": 5, "rows": 1},
            {"date": "2026-06-05", "n_symbols": 5, "rows": 1},
            {"date": "2026-06-10", "n_symbols": 5, "rows": 1},
        ],
    }
    clipped = rc._clip_recent(tier, days=3)  # window = 06-08..06-10
    assert [cell["date"] for cell in clipped["dates"]] == ["2026-06-10"]
    assert clipped["shown_from"] == "2026-06-08"
    assert clipped["n_dates_shown"] == 1
    # full-history depth stats untouched by the window
    assert clipped["span_days"] == 10
    assert clipped["n_dates"] == 3
    # days=None keeps everything
    full = rc._clip_recent(tier, days=None)
    assert len(full["dates"]) == 3


def test_build_raw_coverage_end_to_end(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        "bars",
        [
            {"symbol": "AAA", "date": "2026-06-15", "rows": 380},
            {"symbol": "BBB", "date": "2026-06-16", "rows": 380},
        ],
    )
    _write_manifest(
        tmp_path,
        "trades",
        [{"symbol": "AAA", "date": "2026-06-16", "rows": 5000}],
    )
    out = rc.build_raw_coverage(str(tmp_path), days=0)  # full history
    assert out["store_root"] == str(tmp_path)
    assert out["span_earliest"] == "2026-06-15"
    assert out["span_latest"] == "2026-06-16"
    assert out["anchor_date"] == "2026-06-16"
    layers = {layer["tier"]: layer for layer in out["layers"]}
    assert set(layers) == {"bars", "trades", "quotes"}
    assert layers["bars"]["n_symbols"] == 2
    assert layers["trades"]["n_symbols"] == 1
    assert layers["quotes"]["earliest"] is None  # never acquired -> empty layer, not an error
    assert layers["bars"]["label"] == "minute bars"


def test_cache_serves_and_refreshes(tmp_path: Path) -> None:
    _write_manifest(tmp_path, "bars", [{"symbol": "AAA", "date": "2026-06-15", "rows": 1}])
    cache = rc.RawCoverageCache(ttl=1000.0)
    first = cache.coverage(str(tmp_path), days=0)
    # a second manifest part should NOT show until force refresh (TTL not elapsed)
    _write_manifest(tmp_path, "trades", [{"symbol": "BBB", "date": "2026-06-15", "rows": 1}])
    cached = cache.coverage(str(tmp_path), days=0)
    assert cached is first
    refreshed = cache.coverage(str(tmp_path), days=0, force=True)
    trades = {layer["tier"]: layer for layer in refreshed["layers"]}["trades"]
    assert trades["n_symbols"] == 1
