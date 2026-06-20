"""Unit tests for the date × feature-group coverage MATRIX (services/dashboard/store_grid).

No live DB and no live store: ``trusted_feature_names`` is monkeypatched and a tiny parquet store is built in
a tmp dir, so the matrix the ``/api/store-grid/matrix`` endpoint serves is exercised end-to-end
(build_store_grid all-ticker aggregate per group/date + build_cell_drill per-ticker breakdown) against a
controlled fixture.
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


@pytest.fixture()
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-group catalog so the matrix has stable groups independent of the live registry. groupX has two
    features, groupY one — so groupX is fully-trusted only if BOTH feat_a and feat_b are trusted."""
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


def test_fully_trusted_groups_requires_all_features() -> None:
    catalog = {
        "groupX": [{"feature": "feat_a"}, {"feature": "feat_b"}],
        "groupY": [{"feature": "feat_c"}],
        "empty": [],
    }
    # Only feat_a + feat_c trusted: groupX is NOT fully trusted (feat_b missing), groupY IS.
    fully = sg._fully_trusted_groups(catalog, {"feat_a", "feat_c"})
    assert fully == {"groupY"}
    # Both groupX features trusted -> groupX fully trusted; an empty group never counts.
    fully2 = sg._fully_trusted_groups(catalog, {"feat_a", "feat_b", "feat_c"})
    assert fully2 == {"groupX", "groupY"}


def test_build_store_grid_coverage_and_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    root = tmp_path / "store"
    # 06-16: groupX has AAA+BBB (stream) and groupY has AAA (backfill) -> universe that date = {AAA,BBB} = 2.
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA", "BBB"])
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["AAA"])
    # 06-15: only groupX has AAA -> universe = {AAA} = 1.
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA"])

    # feat_a + feat_b trusted -> groupX fully trusted; feat_c NOT -> groupY untrusted.
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: {"feat_a", "feat_b"})

    grid = sg.build_store_grid(str(root), lookback_days=5)
    assert grid["anchor_date"] == "2026-06-16"
    assert grid["n_groups"] == 2
    assert grid["n_trusted_groups"] == 1  # only groupX
    # Rows are WEEKDAYS only: 06-16 Tue, 06-15 Mon, then 06-14 Sun + 06-13 Sat dropped, 06-12 Fri kept.
    assert grid["dates"] == ["2026-06-16", "2026-06-15", "2026-06-12"]
    # Columns are the GROUPS, trusted-first: groupX (trusted) before groupY.
    assert grid["groups"] == ["groupX", "groupY"]
    assert grid["group_trusted"] == [1, 0]
    # Per-date captured-universe denominator.
    assert grid["universe"] == [2, 1, 0]

    d_idx = {date: i for i, date in enumerate(grid["dates"])}
    g_idx = {group: i for i, group in enumerate(grid["groups"])}
    coverage = grid["coverage"]

    # 06-16 groupX: AAA+BBB of universe 2 -> 2/2 = full byte 255.
    assert coverage[d_idx["2026-06-16"]][g_idx["groupX"]] == 255
    # 06-16 groupY: AAA of universe 2 -> 1/2 -> round(255*0.5)=128.
    assert coverage[d_idx["2026-06-16"]][g_idx["groupY"]] == round(255 * 0.5)
    # 06-15 groupX: AAA of universe 1 -> full 255; groupY absent -> 0.
    assert coverage[d_idx["2026-06-15"]][g_idx["groupX"]] == 255
    assert coverage[d_idx["2026-06-15"]][g_idx["groupY"]] == 0
    # An absent (far-back) date reads zero for every group.
    assert all(byte == 0 for byte in coverage[d_idx["2026-06-12"]])


def test_build_store_grid_thin_group_reads_faint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    root = tmp_path / "store"
    # groupX covers the whole universe; groupY covers 1 of 4 names -> groupY column reads much fainter.
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["A", "B", "C", "D"])
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["A"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())

    grid = sg.build_store_grid(str(root), lookback_days=1)
    g_idx = {group: i for i, group in enumerate(grid["groups"])}
    cov = grid["coverage"][0]
    assert cov[g_idx["groupX"]] == 255  # 4/4
    assert cov[g_idx["groupY"]] == round(255 * 0.25)  # 1/4, faint
    assert grid["group_coverage_pct"][g_idx["groupX"]] == 100.0
    assert grid["group_coverage_pct"][g_idx["groupY"]] == 25.0


def test_build_store_grid_empty_store(monkeypatch: pytest.MonkeyPatch, fake_catalog: None) -> None:
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())
    grid = sg.build_store_grid("/nonexistent/store", lookback_days=3)
    assert grid["anchor_date"] is None
    assert grid["dates"] == []
    assert grid["groups"] == []
    assert grid["coverage"] == []
    assert grid["summary"]["n_dates"] == 0


def test_build_cell_drill_tickers_and_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA", "CCC"])
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["AAA"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: {"feat_a", "feat_b"})  # groupX trusted

    drill = sg.build_cell_drill("groupX", "2026-06-16", str(root), lookback_days=5)
    assert drill["group"] == "groupX"
    assert drill["date"] == "2026-06-16"
    assert drill["trusted"] is True
    assert drill["tickers"] == ["AAA", "CCC"]  # sorted
    assert drill["n_tickers"] == 2
    assert drill["universe"] == 2  # {AAA, CCC} ∪ {AAA}
    assert drill["coverage_pct"] == 100.0  # groupX covers the whole universe that date

    # groupY covers 1 of 2 -> 50%, untrusted.
    drill_y = sg.build_cell_drill("groupY", "2026-06-16", str(root), lookback_days=5)
    assert drill_y["tickers"] == ["AAA"]
    assert drill_y["coverage_pct"] == 50.0
    assert drill_y["trusted"] is False


def test_build_cell_drill_empty_store(monkeypatch: pytest.MonkeyPatch, fake_catalog: None) -> None:
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())
    drill = sg.build_cell_drill("groupX", "2026-06-16", "/nonexistent/store", lookback_days=3)
    assert drill["n_tickers"] == 0
    assert drill["tickers"] == []
