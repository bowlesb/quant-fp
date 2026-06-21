"""Unit tests for the date × column coverage MATRIX (services/dashboard/store_grid, v3).

No live DB and no live store: ``trusted_feature_names`` + ``universe_size`` + the raw-layer read are
monkeypatched and a tiny parquet store is built in a tmp dir, so the matrix the ``/api/store-grid/matrix``
endpoint serves is exercised end-to-end (build_store_grid: raw layers + feature groups against a FIXED
full-universe denominator + build_cell_drill per-ticker breakdown) against a controlled fixture.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import store_grid as sg  # noqa: E402  (path inserted above)


def _write_partition(root: Path, group: str, source: str, date_iso: str, symbols: list[str]) -> None:
    """Write a minimal (symbol, minute, feat) parquet partition the matrix reads symbol sets from."""
    part = root / f"group={group}" / "v=1.0.0" / f"source={source}" / f"date={date_iso}"
    part.mkdir(parents=True, exist_ok=True)
    frame = pl.DataFrame(
        {
            "symbol": symbols,
            "minute": [dt.datetime.fromisoformat(f"{date_iso}T14:30:00")] * len(symbols),
            "feat_value": [1.0] * len(symbols),
        }
    )
    frame.write_parquet(part / "data.parquet")


def _col_index(grid: dict, key: str) -> int:
    return next(i for i, col in enumerate(grid["columns"]) if col["key"] == key)


@pytest.fixture()
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-group catalog, a FIXED universe size of 4, and NO raw layers (so the columns are just the two
    feature groups). groupX has two features (trusted only if both feat_a+feat_b trusted), groupY one."""
    catalog = {
        "groupX": [
            {"feature": "feat_a", "group": "groupX", "version": "1.0.0", "layer": "B"},
            {"feature": "feat_b", "group": "groupX", "version": "1.0.0", "layer": "B"},
        ],
        "groupY": [
            {"feature": "feat_c", "group": "groupY", "version": "1.0.0", "layer": "C"},
        ],
    }
    monkeypatch.setattr(sg, "_catalog_by_group", lambda: catalog)
    monkeypatch.setattr(sg, "_group_version", lambda group: "1.0.0")
    monkeypatch.setattr(sg, "universe_size", lambda: 4)
    monkeypatch.setattr(sg, "RAW_TIERS", [])
    monkeypatch.setattr(sg, "_raw_layer_counts", lambda root, window: {})
    # build_group_info() touches the live registry + the guide YAML — stub it to a fixed shape so the matrix
    # tests stay isolated to the coverage math.
    monkeypatch.setattr(
        sg,
        "build_group_info",
        lambda: {
            "groupX": {
                "docstring": "X",
                "type": "t",
                "layer": "B",
                "n_features": 2,
                "features": [{"name": "feat_a", "description": "a"}],
                "guide": {"value": "honest measure of X"},
            },
            "groupY": {
                "docstring": "Y",
                "type": "t",
                "layer": "C",
                "n_features": 1,
                "features": [{"name": "feat_c", "description": "c"}],
                "guide": None,
            },
        },
    )


def test_fully_trusted_groups_requires_all_features() -> None:
    catalog = {
        "groupX": [{"feature": "feat_a"}, {"feature": "feat_b"}],
        "groupY": [{"feature": "feat_c"}],
        "empty": [],
    }
    fully = sg._fully_trusted_groups(catalog, {"feat_a", "feat_c"})
    assert fully == {"groupY"}
    fully2 = sg._fully_trusted_groups(catalog, {"feat_a", "feat_b", "feat_c"})
    assert fully2 == {"groupX", "groupY"}


def test_coverage_against_fixed_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_env: None
) -> None:
    root = tmp_path / "store"
    # 06-16: groupX covers all 4 names; groupY covers 1 of 4.
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["A", "B", "C", "D"])
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["A"])
    # feat_a + feat_b trusted -> groupX trusted; feat_c not -> groupY untrusted.
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: {"feat_a", "feat_b"})

    grid = sg.build_store_grid(str(root), lookback_days=1)
    assert grid["anchor_date"] == "2026-06-16"
    assert grid["universe_size"] == 4  # the FIXED denominator
    # Columns: trusted-first (groupX before groupY); each has kind="group".
    keys = [col["key"] for col in grid["columns"]]
    assert keys == ["groupX", "groupY"]
    assert grid["columns"][0]["kind"] == "group"
    assert grid["columns"][0]["trusted"] is True
    assert grid["columns"][1]["trusted"] is False
    # Each group carries its features for the horizontal expand.
    assert grid["columns"][0]["features"] == ["feat_a", "feat_b"]

    # The per-group detail-panel info is baked into the matrix (registry-derived + curated guide).
    info = grid["group_info"]
    assert info["groupX"]["guide"]["value"] == "honest measure of X"
    assert info["groupY"]["guide"] is None  # honest stub, not fabricated

    cov = grid["coverage"][0]
    # groupX 4/4 -> 255; groupY 1/4 -> round(255*0.25). Both against the FIXED universe of 4.
    assert cov[_col_index(grid, "groupX")] == 255
    assert cov[_col_index(grid, "groupY")] == round(255 * 0.25)


def test_raw_layers_are_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_env: None) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["A", "B"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())
    # Re-enable raw layers: bars covers 2 of 4 on the anchor date, quotes 4 of 4.
    monkeypatch.setattr(sg, "RAW_TIERS", [("bars", "minute bars"), ("quotes", "tick quotes")])
    monkeypatch.setattr(
        sg,
        "_raw_layer_counts",
        lambda root, window: {"bars": {"2026-06-16": 2}, "quotes": {"2026-06-16": 4}},
    )

    grid = sg.build_store_grid(str(root), lookback_days=1)
    keys = [col["key"] for col in grid["columns"]]
    # Raw layers come FIRST (the substrate), then the feature groups (both registry groups are columns even
    # though only groupX has data this date — groupY renders as a blank column).
    assert keys == ["bars", "quotes", "groupX", "groupY"]
    assert grid["columns"][0]["kind"] == "raw"
    assert grid["columns"][0]["trusted"] is False
    assert grid["summary"]["n_raw"] == 2

    cov = grid["coverage"][0]
    assert cov[_col_index(grid, "bars")] == round(255 * 0.5)  # 2/4
    assert cov[_col_index(grid, "quotes")] == 255  # 4/4
    assert cov[_col_index(grid, "groupX")] == round(255 * 0.5)  # 2/4


def test_build_store_grid_empty_store(monkeypatch: pytest.MonkeyPatch, fake_env: None) -> None:
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())
    grid = sg.build_store_grid("/nonexistent/store", lookback_days=3)
    assert grid["anchor_date"] is None
    assert grid["dates"] == []
    assert grid["columns"] == []
    assert grid["coverage"] == []
    assert grid["summary"]["n_dates"] == 0


def test_build_cell_drill_tickers_and_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_env: None
) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA", "CCC"])
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["AAA"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: {"feat_a", "feat_b"})  # groupX trusted

    drill = sg.build_cell_drill("groupX", "2026-06-16", str(root), lookback_days=5)
    assert drill["group"] == "groupX"
    assert drill["trusted"] is True
    assert drill["tickers"] == ["AAA", "CCC"]  # sorted
    assert drill["n_tickers"] == 2
    assert drill["universe"] == 4  # the FIXED denominator
    assert drill["coverage_pct"] == round(100.0 * 2 / 4, 1)  # 50%

    drill_y = sg.build_cell_drill("groupY", "2026-06-16", str(root), lookback_days=5)
    assert drill_y["tickers"] == ["AAA"]
    assert drill_y["coverage_pct"] == round(100.0 * 1 / 4, 1)  # 25%
    assert drill_y["trusted"] is False


def test_build_cell_drill_empty_store(monkeypatch: pytest.MonkeyPatch, fake_env: None) -> None:
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())
    drill = sg.build_cell_drill("groupX", "2026-06-16", "/nonexistent/store", lookback_days=3)
    assert drill["n_tickers"] == 0
    assert drill["tickers"] == []
    # The source rollup is present and zeroed even with no data (stable shape for the reader).
    assert drill["source_counts"] == sg._ZERO_SOURCE_COUNTS
    assert drill["ticker_sources"] == []


def test_coverage_source_stream_fraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_env: None
) -> None:
    root = tmp_path / "store"
    # groupX: 3 names live (stream) + a 4th present only in backfill -> union of 4, 3/4 stream-present.
    _write_partition(root, "groupX", "stream", "2026-06-16", ["A", "B", "C"])
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["A", "B", "C", "D"])
    # groupY: backfill-only (in history, never captured live) -> stream fraction 0.
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["A", "B"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())

    grid = sg.build_store_grid(str(root), lookback_days=1)
    src = grid["coverage_source"][0]
    # groupX: union 4 tickers, 3 stream-present -> round(255 * 3/4).
    assert src[_col_index(grid, "groupX")] == round(255 * 3 / 4)
    # groupY: backfill-only -> 0 (zero of its covered tickers are live).
    assert src[_col_index(grid, "groupY")] == 0
    # The union coverage byte is unchanged by the split (groupX 4/4 of the universe-4 denom).
    assert grid["coverage"][0][_col_index(grid, "groupX")] == 255


def test_coverage_source_raw_layers_are_na(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_env: None
) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "stream", "2026-06-16", ["A", "B"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())
    monkeypatch.setattr(sg, "RAW_TIERS", [("bars", "minute bars")])
    monkeypatch.setattr(sg, "_raw_layer_counts", lambda root, window: {"bars": {"2026-06-16": 2}})

    grid = sg.build_store_grid(str(root), lookback_days=1)
    src = grid["coverage_source"][0]
    # Raw tape layers have no stream/backfill split -> SOURCE_NA sentinel.
    assert src[_col_index(grid, "bars")] == sg.SOURCE_NA
    # groupX is stream-only -> fully live (255).
    assert src[_col_index(grid, "groupX")] == 255


def test_build_cell_drill_source_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_env: None
) -> None:
    root = tmp_path / "store"
    # AAA + BBB are live AND backfilled (both); CCC is live-only; DDD is backfill-only (the live gap).
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA", "BBB", "CCC"])
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["AAA", "BBB", "DDD"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())

    drill = sg.build_cell_drill("groupX", "2026-06-16", str(root), lookback_days=5)
    assert drill["tickers"] == ["AAA", "BBB", "CCC", "DDD"]  # sorted union
    assert drill["ticker_sources"] == ["both", "both", "stream_only", "backfill_only"]
    counts = drill["source_counts"]
    assert counts["stream"] == 3
    assert counts["backfill"] == 3
    assert counts["both"] == 2
    assert counts["stream_only"] == 1
    assert counts["backfill_only"] == 1
