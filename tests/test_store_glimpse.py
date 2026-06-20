"""Unit tests for the LIVE feature-store glimpse grid (services/dashboard/store_glimpse).

No live DB and no live store: the trust read is monkeypatched and a tiny parquet store is built in a tmp
dir (the SAME fixture shape feature_grid tests use), so the JSON the ``/api/store-glimpse`` and the
``/api/store-glimpse/{group}/tickers`` endpoints serve is exercised end-to-end (build_store_glimpse +
build_ticker_drill) against a controlled fixture. The pure helpers (coverage fraction, trust hue
aggregation, per-day best-source count) are tested directly.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import store_glimpse as sg  # noqa: E402  (path inserted above)


class _FT:
    """Minimal trust row: the glimpse only reads ``.lifecycle_state``."""

    def __init__(self, feature: str, lifecycle_state: str) -> None:
        self.feature = feature
        self.lifecycle_state = lifecycle_state


def _trust(feature: str, lifecycle: str) -> _FT:
    """A FeatureTrust-shaped stand-in for monkeypatching ``trust_by_feature``."""
    return _FT(feature, lifecycle)


def _write_partition(root: Path, group: str, source: str, date_iso: str, symbols: list[str]) -> None:
    """Write a minimal (symbol, minute, <feat>) parquet partition the glimpse reads symbol counts from."""
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
    """A two-group catalog so the glimpse has stable columns independent of the live registry."""
    rows = [
        {"feature": "feat_a", "group": "groupX", "version": "1.0.0", "layer": "B"},
        {"feature": "feat_b", "group": "groupX", "version": "1.0.0", "layer": "B"},
        {"feature": "feat_c", "group": "groupY", "version": "1.0.0", "layer": "C"},
    ]
    monkeypatch.setattr(
        sg,
        "_catalog_by_group",
        lambda: {
            "groupX": [r for r in rows if r["group"] == "groupX"],
            "groupY": [r for r in rows if r["group"] == "groupY"],
        },
    )
    monkeypatch.setattr(sg, "_group_version", lambda group: "1.0.0")


def test_coverage_fraction_clamps_and_floors() -> None:
    assert sg._coverage_fraction(3659, 7318) == 0.5
    assert sg._coverage_fraction(0, 7318) == 0.0
    assert sg._coverage_fraction(9000, 7318) == 1.0  # never exceeds 1.0
    assert sg._coverage_fraction(100, 0) == 0.0  # no universe -> 0, not a div-by-zero


def test_trust_hue_mapping() -> None:
    assert sg._trust_hue("VALIDATED") == "trusted"
    assert sg._trust_hue("PENDING") == "pending"
    assert sg._trust_hue("DIVERGENT") == "divergent"
    assert sg._trust_hue("UNGRADED") == "ungraded"
    assert sg._trust_hue("RETIRED") == "ungraded"
    assert sg._trust_hue("WHATEVER") == "ungraded"  # unknown defaults to grey


def test_aggregate_hue_worst_actionable_first() -> None:
    # DIVERGENT dominates everything.
    assert sg._aggregate_hue(["VALIDATED", "DIVERGENT", "PENDING"]) == "divergent"
    # else any VALIDATED -> trusted.
    assert sg._aggregate_hue(["UNGRADED", "VALIDATED", "PENDING"]) == "trusted"
    # else any PENDING -> pending.
    assert sg._aggregate_hue(["UNGRADED", "PENDING"]) == "pending"
    # else ungraded.
    assert sg._aggregate_hue(["UNGRADED", "RETIRED"]) == "ungraded"
    assert sg._aggregate_hue([]) == "ungraded"


def test_day_count_takes_best_source() -> None:
    per_date = {"stream": {"2026-06-16": 2}, "backfill": {"2026-06-16": 5}}
    assert sg._day_count(per_date, "2026-06-16") == 5  # max(stream, backfill)
    assert sg._day_count(per_date, "2026-06-15") == 0  # absent day


def test_build_store_glimpse_cells_coverage_and_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    root = tmp_path / "store"
    # groupX: stream 06-16 (2 syms) + backfill 06-16 (4 syms) and 06-15 (3 syms).
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA", "BBB"])
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["AAA", "BBB", "CCC", "DDD"])
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA", "BBB", "CCC"])
    # groupY: backfill only on the anchor day (1 sym).
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["AAA"])

    # feat_a VALIDATED, feat_b DIVERGENT (groupX); feat_c UNGRADED (groupY).
    monkeypatch.setattr(
        sg,
        "trust_by_feature",
        lambda: {
            "feat_a": _trust("feat_a", "VALIDATED"),
            "feat_b": _trust("feat_b", "DIVERGENT"),
        },
    )

    view = sg.build_store_glimpse(str(root), days=5, universe_size=8)
    assert view["anchor_date"] == "2026-06-16"
    assert view["universe_size"] == 8
    assert view["dates"][0] == "2026-06-16"  # newest first
    assert len(view["dates"]) == 5

    summary = view["summary"]
    assert summary["n_groups"] == 2
    assert summary["n_features"] == 3
    assert summary["n_trusted"] == 1  # only feat_a
    # trust_counts: feat_a trusted, feat_b divergent, feat_c ungraded.
    assert summary["trust_counts"] == {"trusted": 1, "pending": 0, "divergent": 1, "ungraded": 1}

    # groupX column: feat_b is DIVERGENT -> the group cell hue is divergent (worst-first).
    gx = next(g for g in view["groups"] if g["group"] == "groupX")
    assert gx["trust_hue"] == "divergent"
    assert gx["n_features"] == 2
    assert {f["feature"]: f["trust_hue"] for f in gx["features"]} == {
        "feat_a": "trusted",
        "feat_b": "divergent",
    }

    cells = view["cells"]
    # 06-16 groupX: best source = backfill 4 syms / universe 8 = 0.5 darkness, divergent hue.
    gx_1616 = cells["2026-06-16"]["groupX"]
    assert gx_1616["n_symbols"] == 4
    assert gx_1616["coverage"] == 0.5
    assert gx_1616["hue"] == "divergent"
    # 06-15 groupX: backfill 3 / 8 = 0.375.
    assert cells["2026-06-15"]["groupX"]["coverage"] == 0.375
    # groupY 06-16: 1 sym / 8 = 0.125, ungraded.
    gy_1616 = cells["2026-06-16"]["groupY"]
    assert gy_1616["n_symbols"] == 1
    assert gy_1616["coverage"] == 0.125
    assert gy_1616["hue"] == "ungraded"
    # Total column 06-16: max symbols across groups = 4 (groupX backfill) / 8 = 0.5; hue = worst over groups
    # (groupX divergent) -> divergent.
    tot = cells["2026-06-16"]["__total__"]
    assert tot["n_symbols"] == 4
    assert tot["coverage"] == 0.5
    assert tot["hue"] == "divergent"
    # An absent date (no partitions) reads zero coverage everywhere.
    oldest = view["dates"][-1]
    assert cells[oldest]["groupX"]["coverage"] == 0.0
    assert cells[oldest]["__total__"]["coverage"] == 0.0


def test_build_store_glimpse_empty_store(monkeypatch: pytest.MonkeyPatch, fake_catalog: None) -> None:
    monkeypatch.setattr(sg, "trust_by_feature", lambda: {})
    view = sg.build_store_glimpse("/nonexistent/store", days=3)
    assert view["anchor_date"] is None
    assert view["dates"] == []
    assert view["cells"] == {}
    assert view["summary"]["n_dates"] == 0


def test_build_ticker_drill_presence_and_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    root = tmp_path / "store"
    # groupX over two dates. AAA present both days both sources; BBB only backfill 06-16; CCC only stream 06-15.
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["AAA", "BBB"])
    _write_partition(root, "groupX", "stream", "2026-06-15", ["AAA", "CCC"])
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA"])

    monkeypatch.setattr(sg, "trust_by_feature", lambda: {})

    drill = sg.build_ticker_drill("groupX", str(root), days=5, limit=10)
    assert drill["group"] == "groupX"
    assert drill["anchor_date"] == "2026-06-16"
    assert drill["dates"][0] == "2026-06-16"
    assert drill["n_tickers"] == 3

    by_symbol = {t["symbol"]: t for t in drill["tickers"]}
    # AAA present on both dates -> ranked FIRST (most-covered).
    assert drill["tickers"][0]["symbol"] == "AAA"
    assert by_symbol["AAA"]["n_present"] == 2
    # AAA on 06-16 = both sources -> 'both'; on 06-15 = stream(AAA)+backfill(AAA) -> 'both'.
    aaa_boxes = {b["date"]: b["provenance"] for b in by_symbol["AAA"]["boxes"]}
    assert aaa_boxes["2026-06-16"] == "both"
    assert aaa_boxes["2026-06-15"] == "both"
    # BBB only backfill 06-16.
    bbb_boxes = {b["date"]: b["provenance"] for b in by_symbol["BBB"]["boxes"]}
    assert bbb_boxes["2026-06-16"] == "backfill"
    # CCC only stream 06-15.
    ccc_boxes = {b["date"]: b["provenance"] for b in by_symbol["CCC"]["boxes"]}
    assert ccc_boxes["2026-06-15"] == "stream"
    # dates with no partition for a symbol read 'absent'.
    assert {b["provenance"] for b in by_symbol["CCC"]["boxes"]} >= {"stream", "absent"}


def test_build_ticker_drill_limit_paginates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "backfill", "2026-06-16", ["AAA", "BBB", "CCC", "DDD", "EEE"])
    monkeypatch.setattr(sg, "trust_by_feature", lambda: {})
    drill = sg.build_ticker_drill("groupX", str(root), days=2, limit=2)
    assert drill["n_tickers"] == 5  # total reported
    assert len(drill["tickers"]) == 2  # but only `limit` rows returned
    assert drill["limit"] == 2


def test_build_ticker_drill_unknown_group_raises(fake_catalog: None) -> None:
    with pytest.raises(KeyError):
        sg.build_ticker_drill("nope", "/tmp/store", days=2)
