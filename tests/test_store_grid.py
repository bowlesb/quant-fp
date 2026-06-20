"""Unit tests for the ticker×date feature-store coverage MATRIX (services/dashboard/store_grid).

No live DB and no live store: ``trusted_feature_names`` is monkeypatched and a tiny parquet store is built in
a tmp dir (the SAME fixture shape the feature_grid / store_glimpse tests use), so the matrix the
``/api/store-grid/matrix`` endpoint serves is exercised end-to-end (build_store_grid + build_ticker_drill)
against a controlled fixture.
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
    # groupX present 06-16 (AAA via stream, BBB via backfill) and 06-15 (AAA backfill).
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["BBB"])
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA"])
    # groupY present 06-16 (AAA backfill only).
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["AAA"])

    # feat_a + feat_b trusted -> groupX fully trusted; feat_c NOT -> groupY untrusted.
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: {"feat_a", "feat_b"})

    grid = sg.build_store_grid(str(root), lookback_days=5)
    assert grid["anchor_date"] == "2026-06-16"
    assert grid["n_groups"] == 2
    assert grid["n_trusted_groups"] == 1  # only groupX
    assert grid["dates"][0] == "2026-06-16"  # newest first
    # Rows are WEEKDAYS only: 06-16 Tue, 06-15 Mon, then 06-14 Sun + 06-13 Sat dropped, 06-12 Fri kept.
    assert grid["dates"] == ["2026-06-16", "2026-06-15", "2026-06-12"]

    tickers = list(grid["tickers"])
    assert set(tickers) == {"AAA", "BBB"}
    d_idx = {date: i for i, date in enumerate(grid["dates"])}
    t_idx = {sym: i for i, sym in enumerate(tickers)}

    coverage = grid["coverage"]
    trusted = grid["trusted"]

    # 06-16 AAA: present in groupX (stream) + groupY (backfill) = 2 of 2 groups -> full coverage byte 255.
    aaa_1616 = coverage[d_idx["2026-06-16"]][t_idx["AAA"]]
    assert aaa_1616 == 255
    # AAA on 06-16 is covered by groupY (untrusted) -> cell trust bit 0.
    assert trusted[d_idx["2026-06-16"]][t_idx["AAA"]] == 0

    # 06-16 BBB: only groupX (trusted) of 2 groups -> coverage 0.5 -> round(255*0.5)=128, trust bit 1.
    bbb_1616 = coverage[d_idx["2026-06-16"]][t_idx["BBB"]]
    assert bbb_1616 == round(255 * 0.5)
    assert trusted[d_idx["2026-06-16"]][t_idx["BBB"]] == 1

    # 06-15 AAA: only groupX of 2 groups -> coverage 0.5; groupX trusted -> bit 1.
    aaa_1615 = coverage[d_idx["2026-06-15"]][t_idx["AAA"]]
    assert aaa_1615 == round(255 * 0.5)
    assert trusted[d_idx["2026-06-15"]][t_idx["AAA"]] == 1

    # An absent (far-back) date reads zero coverage for every ticker.
    oldest = grid["dates"][-1]
    assert all(byte == 0 for byte in coverage[d_idx[oldest]])


def test_build_store_grid_default_sort_most_covered_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    root = tmp_path / "store"
    # WIDE present in both groups every day; THIN present in one group one day -> WIDE sorts first.
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["WIDE", "THIN"])
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["WIDE"])
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["WIDE"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())

    grid = sg.build_store_grid(str(root), lookback_days=3)
    assert grid["tickers"][0] == "WIDE"  # higher mean coverage ranks first
    assert grid["coverage_pct"][0] >= grid["coverage_pct"][1]


def test_build_store_grid_empty_store(monkeypatch: pytest.MonkeyPatch, fake_catalog: None) -> None:
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())
    grid = sg.build_store_grid("/nonexistent/store", lookback_days=3)
    assert grid["anchor_date"] is None
    assert grid["dates"] == []
    assert grid["tickers"] == []
    assert grid["coverage"] == []
    assert grid["summary"]["n_dates"] == 0


def test_build_ticker_drill_presence_and_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["AAA"])
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA"])
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: {"feat_a", "feat_b"})  # groupX trusted

    drill = sg.build_ticker_drill("aaa", str(root), lookback_days=5)  # lowercase -> upper
    assert drill["symbol"] == "AAA"
    assert drill["anchor_date"] == "2026-06-16"
    assert drill["dates"][0] == "2026-06-16"
    group_trust = {g["group"]: g["trusted"] for g in drill["groups"]}
    assert group_trust == {"groupX": True, "groupY": False}
    # 06-16: AAA in both groups; 06-15: AAA only in groupX.
    assert drill["cells"]["2026-06-16"] == {"groupX": True, "groupY": True}
    assert drill["cells"]["2026-06-15"] == {"groupX": True}


def test_build_ticker_drill_empty_store(monkeypatch: pytest.MonkeyPatch, fake_catalog: None) -> None:
    monkeypatch.setattr(sg, "trusted_feature_names", lambda: set())
    drill = sg.build_ticker_drill("AAA", "/nonexistent/store", lookback_days=3)
    assert drill["anchor_date"] is None
    assert drill["dates"] == []
    assert drill["cells"] == {}
