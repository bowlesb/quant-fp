"""Unit tests for the dashboard feature-coverage + trust grid aggregation (services/dashboard/feature_grid).

No live DB and no live store: the trust read is monkeypatched and a tiny parquet store is built in a tmp
dir, so the JSON the ``/api/feature-grid`` endpoint serves is exercised end-to-end (build_grid +
build_group_detail) against a controlled fixture. The pure helpers (coverage maths, trading-day count,
period windows, trust aggregation) are tested directly.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "dashboard"))

import feature_grid as fg  # noqa: E402  (path inserted above)


def test_trading_weekdays_counts_mon_to_fri() -> None:
    # 2026-06-15 is a Monday; through Friday 2026-06-19 = 5 weekdays.
    assert fg.trading_weekdays(dt.date(2026, 6, 15), dt.date(2026, 6, 19)) == 5
    # spanning a weekend: Fri 06-19 .. Mon 06-22 = Fri + Mon = 2.
    assert fg.trading_weekdays(dt.date(2026, 6, 19), dt.date(2026, 6, 22)) == 2
    assert fg.trading_weekdays(dt.date(2026, 6, 20), dt.date(2026, 6, 15)) == 0


def test_coverage_for_source_peak_universe_denominator() -> None:
    # Two dates: 100 symbols then 50. Peak universe = 100; expected = 100 * max(n_trading_days, n_dates).
    per_date = {"2026-06-15": 100, "2026-06-16": 50}
    cov = fg.coverage_for_source(per_date, dt.date(2026, 6, 15), dt.date(2026, 6, 16), n_trading_days=2)
    assert cov.symbol_days == 150
    assert cov.expected_symbol_days == 200  # 100 peak * 2 days
    assert cov.pct == 75.0
    assert cov.first_date == "2026-06-15"
    assert cov.last_date == "2026-06-16"
    assert cov.n_dates == 2


def test_coverage_for_source_empty_window() -> None:
    cov = fg.coverage_for_source({"2026-01-01": 10}, dt.date(2026, 6, 1), dt.date(2026, 6, 2), 2)
    assert cov.pct == 0.0
    assert cov.expected_symbol_days == 0


def test_period_window_fixed_unclamped_all_clamps_to_floor() -> None:
    anchor = dt.date(2026, 6, 16)
    floor = dt.date(2026, 6, 15)
    # FIXED lookback is NOT clamped to floor: it uses the true window edge so a long row over a short store
    # reads near-empty (temporal depth), instead of collapsing onto the same captured days as shorter rows.
    start, end = fg.period_window("12m", 365, anchor, floor)
    assert (start, end) == (anchor - dt.timedelta(days=364), anchor)
    # "all history" clamps to floor (earliest captured date) — pre-capture days are not "missing".
    start, end = fg.period_window("all", None, anchor, floor)
    assert (start, end) == (floor, anchor)
    start, end = fg.period_window("1d", 1, anchor, floor)
    assert (start, end) == (anchor, anchor)


def test_aggregate_trust_states() -> None:
    state, pct, n_trusted, n_validating, n_ungraded = fg._aggregate_trust(["UNGRADED", "UNGRADED"])
    assert (state, pct, n_trusted) == ("UNGRADED", 0.0, 0)
    state, pct, n_trusted, _, _ = fg._aggregate_trust(["VALIDATED", "VALIDATED"])
    assert (state, pct, n_trusted) == ("VALIDATED", 100.0, 2)
    state, pct, n_trusted, n_validating, _ = fg._aggregate_trust(["VALIDATED", "UNGRADED"])
    assert state == "VALIDATED" and pct == 50.0 and n_trusted == 1
    state, *_ = fg._aggregate_trust(["DIVERGENT", "VALIDATED"])
    assert state == "DIVERGENT"  # any divergent dominates the badge
    state, _, _, n_validating, _ = fg._aggregate_trust(["PENDING", "UNGRADED"])
    assert state == "PENDING" and n_validating == 1


def _write_partition(root: Path, group: str, source: str, date_iso: str, symbols: list[str]) -> None:
    """Write a minimal (symbol, minute, <feat>) parquet partition the grid reads symbol counts from."""
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
    """A two-group catalog so the grid has stable columns independent of the live registry."""
    rows = [
        {"feature": "feat_a", "group": "groupX", "version": "1.0.0", "layer": "B",
         "parity_method": "tolerance", "description": "Feature A description for hover testing."},
        {"feature": "feat_b", "group": "groupX", "version": "1.0.0", "layer": "B",
         "parity_method": "tolerance", "description": "Feature B description for hover testing."},
        {"feature": "feat_c", "group": "groupY", "version": "1.0.0", "layer": "C",
         "parity_method": "tolerance", "description": "Feature C description for hover testing."},
    ]
    monkeypatch.setattr(fg, "_catalog_by_group", lambda: {
        "groupX": [r for r in rows if r["group"] == "groupX"],
        "groupY": [r for r in rows if r["group"] == "groupY"],
    })
    monkeypatch.setattr(fg, "_group_version", lambda group: "1.0.0")


def test_build_grid_against_tmp_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None) -> None:
    root = tmp_path / "store"
    # groupX: stream on both days (3 then 2 symbols), backfill on day 1 only (3 symbols).
    _write_partition(root, "groupX", "stream", "2026-06-15", ["AAA", "BBB", "CCC"])
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA", "BBB"])
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA", "BBB", "CCC"])
    # groupY: backfill only on day 2.
    _write_partition(root, "groupY", "backfill", "2026-06-16", ["AAA"])

    # feat_a VALIDATED, feat_b PENDING (groupX); feat_c UNGRADED (groupY).
    monkeypatch.setattr(fg, "trust_by_feature", lambda: {
        "feat_a": fg.FeatureTrust("feat_a", "VALIDATED", 3, 3, 0.9999, "2026-06-16"),
        "feat_b": fg.FeatureTrust("feat_b", "PENDING", 1, 1, 0.999, "2026-06-15"),
    })

    grid = fg.build_grid(str(root))
    assert grid["anchor_date"] == "2026-06-16"
    assert grid["earliest_date"] == "2026-06-15"
    assert {g["group"] for g in grid["groups"]} == {"groupX", "groupY"}
    assert grid["summary"]["n_features"] == 3
    assert grid["summary"]["n_trusted"] == 1  # only feat_a
    assert grid["summary"]["n_groups"] == 2

    cells = {(c["group"], c["period"]): c for c in grid["cells"]}
    # groupX 'all' period: stream symbol-days = 3+2=5, peak universe 3, expected = 3*2 = 6 -> 83.3%.
    gx_all = cells[("groupX", "all")]
    assert gx_all["stream_pct"] == pytest.approx(83.3, abs=0.1)
    assert gx_all["backfill_pct"] == pytest.approx(50.0, abs=0.1)  # 3 / (3*2)
    assert gx_all["coverage_pct"] == pytest.approx(83.3, abs=0.1)  # max(stream, backfill)
    # groupX trust badge: feat_a VALIDATED + feat_b PENDING -> VALIDATED with 50% trusted.
    assert gx_all["trust_state"] == "VALIDATED"
    assert gx_all["trust_pct"] == 50.0
    # groupY is entirely UNGRADED.
    assert cells[("groupY", "all")]["trust_state"] == "UNGRADED"
    assert cells[("groupY", "all")]["trust_pct"] == 0.0
    # '1d' period (anchor day only): groupX has stream (2 symbols) but no backfill on 06-16.
    gx_1d = cells[("groupX", "1d")]
    assert gx_1d["stream_pct"] == 100.0  # 2 symbols / (2 peak * 1 day)
    assert gx_1d["backfill_pct"] == 0.0


def test_build_group_detail_trajectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_catalog: None) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "stream", "2026-06-15", ["AAA", "BBB"])
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA", "BBB"])

    monkeypatch.setattr(fg, "trust_by_feature", lambda: {
        "feat_a": fg.FeatureTrust("feat_a", "VALIDATED", 3, 3, 0.99995, "2026-06-16"),
        "feat_b": fg.FeatureTrust("feat_b", "PENDING", 1, 1, 0.998, "2026-06-15"),
    })

    detail = fg.build_group_detail("groupX", str(root))
    assert detail["group"] == "groupX"
    assert detail["n_features"] == 2
    assert detail["stream_dates"] == ["2026-06-15", "2026-06-16"]
    assert detail["backfill_dates"] == ["2026-06-15"]
    # 06-16 is stream-only -> not parity-checkable; surfaced as the "why not trusted" gap.
    assert detail["stream_only_dates"] == ["2026-06-16"]
    assert detail["backfill_only_dates"] == []

    by_name = {f["feature"]: f for f in detail["features"]}
    assert by_name["feat_a"]["trust_state"] == "VALIDATED"
    assert by_name["feat_a"]["progress_to_trusted_pct"] == 100.0
    # feat_b PENDING with 1 clean day of 2 needed -> 50% to trusted.
    assert by_name["feat_b"]["trust_state"] == "PENDING"
    assert by_name["feat_b"]["clean_days"] == 1
    assert by_name["feat_b"]["days_needed"] == fg.DAYS_NEEDED_FOR_TRUST
    assert by_name["feat_b"]["progress_to_trusted_pct"] == 50.0


def test_build_group_detail_unknown_group(monkeypatch: pytest.MonkeyPatch, fake_catalog: None) -> None:
    with pytest.raises(KeyError):
        fg.build_group_detail("no_such_group", "/store")


def test_latest_partition_date(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-17", ["AAA"])
    assert fg._latest_partition_date(str(root), "groupX", "1.0.0", "backfill") == "2026-06-17"
    # a source with no partitions returns None (groupX has no stream tree written here).
    assert fg._latest_partition_date(str(root), "groupX", "1.0.0", "stream") is None


def test_build_symbol_coverage_classifies_under_representation(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    # The order-flow story in miniature: backfill agg covers a wide universe on its latest date, but the live
    # stream's latest date captured only a thin subset -> the rest is under-represented LIVE.
    _write_partition(root, "groupX", "stream", "2026-06-18", ["AAA", "BBB", "EEE"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB", "CCC", "DDD"])

    cov = fg.build_symbol_coverage("groupX", str(root))
    assert cov["group"] == "groupX"
    assert cov["stream_date"] == "2026-06-18"
    assert cov["backfill_date"] == "2026-06-18"
    assert cov["n_stream"] == 3 and cov["n_backfill"] == 4
    assert cov["both"] == ["AAA", "BBB"]
    # CCC/DDD are in backfill but were not captured live -> under-represented LIVE.
    assert cov["backfill_only"] == ["CCC", "DDD"]
    assert cov["n_backfill_only"] == 2
    # EEE streamed but is absent from today's backfill.
    assert cov["stream_only"] == ["EEE"]
    # union = {AAA,BBB,CCC,DDD,EEE} = 5; stream captured 3 -> 60%.
    assert cov["stream_coverage_pct"] == pytest.approx(60.0, abs=0.1)


def test_build_symbol_coverage_uses_each_sources_own_latest_date(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    # Stream lags a day behind backfill: each side compares its OWN freshest captured universe.
    _write_partition(root, "groupX", "stream", "2026-06-17", ["AAA", "BBB"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB", "CCC"])
    cov = fg.build_symbol_coverage("groupX", str(root))
    assert cov["stream_date"] == "2026-06-17"
    assert cov["backfill_date"] == "2026-06-18"
    assert cov["backfill_only"] == ["CCC"]


def test_build_symbol_coverage_unknown_group(fake_catalog: None) -> None:
    with pytest.raises(KeyError):
        fg.build_symbol_coverage("no_such_group", "/store")


def test_build_thin_live_symbols_ranks_cross_group(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    # groupX: stream carries AAA only; backfill has AAA,BBB,CCC -> BBB,CCC under-represented LIVE here.
    _write_partition(root, "groupX", "stream", "2026-06-18", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB", "CCC"])
    # groupY: stream carries AAA,BBB; backfill has AAA,BBB,CCC -> only CCC under-represented LIVE here.
    _write_partition(root, "groupY", "stream", "2026-06-18", ["AAA", "BBB"])
    _write_partition(root, "groupY", "backfill", "2026-06-18", ["AAA", "BBB", "CCC"])

    rollup = fg.build_thin_live_symbols(str(root))
    assert rollup["n_live_groups"] == 2 and rollup["n_groups"] == 2
    by_symbol = {row["symbol"]: row for row in rollup["symbols"]}
    # CCC is under-represented in BOTH groups -> ranks first; BBB only in groupX.
    assert rollup["symbols"][0]["symbol"] == "CCC"
    assert by_symbol["CCC"]["n_under_groups"] == 2
    assert by_symbol["CCC"]["under_groups"] == ["groupX", "groupY"]
    assert by_symbol["BBB"]["n_under_groups"] == 1
    assert by_symbol["BBB"]["under_groups"] == ["groupX"]
    # AAA is on both streams -> never under-represented, absent from the ranked list.
    assert "AAA" not in by_symbol
    assert rollup["n_thin_symbols"] == 2


def test_build_thin_live_symbols_excludes_non_live_groups(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    # groupX is live (stream AAA); groupY is backfill-only (no stream) -> its whole universe must NOT count
    # as under-represented (it was never live-subscribed), so DDD/EEE never enter the ranking.
    _write_partition(root, "groupX", "stream", "2026-06-18", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB"])
    _write_partition(root, "groupY", "backfill", "2026-06-18", ["AAA", "DDD", "EEE"])

    rollup = fg.build_thin_live_symbols(str(root))
    assert rollup["n_live_groups"] == 1
    symbols = {row["symbol"] for row in rollup["symbols"]}
    # Only groupX's backfill-only BBB is thin-live; groupY's DDD/EEE are excluded (group not live).
    assert symbols == {"BBB"}
    group_rows = {row["group"]: row for row in rollup["groups"]}
    assert group_rows["groupY"]["live"] is False and group_rows["groupY"]["n_under"] == 0
    assert group_rows["groupX"]["live"] is True and group_rows["groupX"]["n_under"] == 1


def test_build_thin_live_symbols_limit_caps_list(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "stream", "2026-06-18", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB", "CCC", "DDD"])
    rollup = fg.build_thin_live_symbols(str(root), limit=2)
    # 3 thin symbols exist (BBB,CCC,DDD) but the ranked list is capped at the limit.
    assert rollup["n_thin_symbols"] == 3
    assert len(rollup["symbols"]) == 2
    assert rollup["limit"] == 2


def test_day_provenance_classifies_all_four() -> None:
    assert fg._day_provenance(5, 5) == "both"
    assert fg._day_provenance(5, 0) == "stream_only"
    assert fg._day_provenance(0, 5) == "backfill_only"
    assert fg._day_provenance(0, 0) == "absent"


def test_stream_horizon_skips_weekends_and_stops_at_gap() -> None:
    # 2026-06-18 is a Thursday. A Mon(15)-Thu(18) capture streak reads as 4 weekday horizon.
    anchor = dt.date(2026, 6, 18)
    streak = ["2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18"]
    assert fg._stream_horizon_days(streak, anchor) == 4
    # A weekday gap stops the walk: missing Wed(17) → only Thu(18) counts unbroken from the anchor.
    with_gap = ["2026-06-15", "2026-06-16", "2026-06-18"]
    assert fg._stream_horizon_days(with_gap, anchor) == 1
    # No capture on the anchor weekday → horizon 0.
    assert fg._stream_horizon_days(["2026-06-15"], anchor) == 0
    # A Fri-anchor streak crossing the weekend back to the prior Fri still reads its true weekday depth.
    fri = dt.date(2026, 6, 19)
    across = ["2026-06-12", "2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18", "2026-06-19"]
    assert fg._stream_horizon_days(across, fri) == 6


def test_build_coverage_timeline_presence_and_depth(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    # groupX: deep backfill history (06-10 → 06-18) + a 2-day live stream streak ending at the anchor.
    _write_partition(root, "groupX", "backfill", "2026-06-10", ["AAA", "BBB"])
    _write_partition(root, "groupX", "backfill", "2026-06-17", ["AAA", "BBB", "CCC"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB", "CCC"])
    _write_partition(root, "groupX", "stream", "2026-06-17", ["AAA", "BBB"])
    _write_partition(root, "groupX", "stream", "2026-06-18", ["AAA"])
    # groupY: backfill-only on the anchor day (no live stream at all).
    _write_partition(root, "groupY", "backfill", "2026-06-18", ["AAA"])

    view = fg.build_coverage_timeline(str(root), days=10)
    assert view["anchor_date"] == "2026-06-18"
    assert view["days"] == 10
    assert view["dates"][0] == "2026-06-18"  # most-recent first
    assert len(view["dates"]) == 10

    by_group = {g["group"]: g for g in view["groups"]}
    gx = by_group["groupX"]
    # History depth: 06-10 → 06-18 inclusive = 9 calendar days.
    assert gx["backfill_earliest"] == "2026-06-10"
    assert gx["backfill_latest"] == "2026-06-18"
    assert gx["backfill_span_days"] == 9
    # Live horizon: Wed 06-17 + Thu 06-18 captured unbroken from the anchor = 2 weekdays.
    assert gx["stream_horizon_days"] == 2
    # In-window peak symbol counts per source (drives the page's coverage-volume heat intensity):
    # stream busiest day = 06-17 (AAA, BBB = 2); backfill busiest = 06-17/06-18 (3).
    assert gx["stream_peak"] == 2
    assert gx["backfill_peak"] == 3

    gx_days = {d["date"]: d for d in gx["days"]}
    # 06-18: stream(1) + backfill(3) both present.
    assert gx_days["2026-06-18"]["provenance"] == "both"
    assert gx_days["2026-06-18"]["stream"] == 1 and gx_days["2026-06-18"]["backfill"] == 3
    # 06-10: backfill only (deep history, no live capture that day).
    assert gx_days["2026-06-10"]["provenance"] == "backfill_only"
    # 06-13 (a Saturday inside the window): neither source -> absent.
    assert gx_days["2026-06-13"]["provenance"] == "absent"

    gy = by_group["groupY"]
    assert gy["stream_horizon_days"] == 0  # never live
    gy_days = {d["date"]: d for d in gy["days"]}
    assert gy_days["2026-06-18"]["provenance"] == "backfill_only"
    # No live stream ever -> stream peak is 0 (cells render dark, no false heat).
    assert gy["stream_peak"] == 0
    assert gy["backfill_peak"] == 1


def test_build_coverage_timeline_peaks_track_in_window_max(tmp_path: Path, fake_catalog: None) -> None:
    """Per-group peaks are the busiest in-window day per source — the heat-normalization denominator.
    A thinning live stream (3 syms today vs a 9-sym peak earlier in the window) keeps the high peak so
    the recent thin days render dimmer, making the coverage drop legible."""
    root = tmp_path / "store"
    _write_partition(root, "groupX", "stream", "2026-06-16", ["AAA", "BBB", "CCC"])  # 3
    _write_partition(root, "groupX", "stream", "2026-06-17", ["A", "B", "C", "D", "E", "F", "G", "H", "I"])  # 9 peak
    _write_partition(root, "groupX", "stream", "2026-06-18", ["AAA"])  # thinned to 1
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB"])

    view = fg.build_coverage_timeline(str(root), days=10)
    gx = {g["group"]: g for g in view["groups"]}["groupX"]
    assert gx["stream_peak"] == 9  # the 06-17 busiest day, not today's thinned 1
    assert gx["backfill_peak"] == 2


def test_detect_stream_drop_severe_and_thinning() -> None:
    """detect_stream_drop classifies a group's RECENT live capture vs its own in-window peak.
    The motivating case: asset_flags peaked ~10381, then recent days run ~2820 (ratio ~0.27) -> hard DROP."""
    dates = ["2026-06-18", "2026-06-17", "2026-06-16", "2026-06-15"]  # most-recent first
    # Severe: recent 2820 vs peak 10381 -> ratio 0.27 <= DROP_SEVERE_RATIO -> "drop".
    severe = {"2026-06-15": 10381, "2026-06-16": 2820, "2026-06-17": 2820, "2026-06-18": 2820}
    verdict = fg.detect_stream_drop(severe, dates, stream_peak=10381)
    assert verdict["status"] == "drop"
    assert verdict["recent"] == 2820 and verdict["recent_date"] == "2026-06-18"
    assert verdict["baseline"] == 10381
    assert verdict["ratio"] == round(2820 / 10381, 3)

    # Thinning (not severe): recent 550 vs peak 1000 -> ratio 0.55, between SEVERE(0.4) and THINNING(0.6).
    mild = {"2026-06-15": 1000, "2026-06-18": 550}
    assert fg.detect_stream_drop(mild, dates, stream_peak=1000)["status"] == "thinning"


def test_detect_stream_drop_ok_and_small_baseline() -> None:
    dates = ["2026-06-18", "2026-06-17", "2026-06-16"]
    # Healthy: recent ~ peak -> ok.
    full = {"2026-06-16": 1000, "2026-06-18": 990}
    assert fg.detect_stream_drop(full, dates, stream_peak=1000)["status"] == "ok"
    # Small baseline (a thin canary order-flow group 24 -> 8): below DROP_MIN_BASELINE_SYMBOLS -> never alarmed.
    canary = {"2026-06-16": 24, "2026-06-18": 8}
    assert fg.detect_stream_drop(canary, dates, stream_peak=24)["status"] == "ok"
    # No stream at all -> no_stream verdict, never an alert.
    assert fg.detect_stream_drop({}, dates, stream_peak=0)["status"] == "no_stream"


def test_build_coverage_timeline_drop_alerts(tmp_path: Path, fake_catalog: None) -> None:
    """A group whose live stream thinned from a large peak surfaces in the window-level drop_alerts list."""
    root = tmp_path / "store"
    # groupX: 200-symbol live peak on 06-16, thinned to 40 on the anchor (ratio 0.2 -> hard DROP).
    _write_partition(root, "groupX", "stream", "2026-06-16", [f"S{i}" for i in range(200)])
    _write_partition(root, "groupX", "stream", "2026-06-18", [f"S{i}" for i in range(40)])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA"])
    # groupY: steady 150-symbol live capture -> no alert.
    _write_partition(root, "groupY", "stream", "2026-06-17", [f"T{i}" for i in range(150)])
    _write_partition(root, "groupY", "stream", "2026-06-18", [f"T{i}" for i in range(148)])

    view = fg.build_coverage_timeline(str(root), days=10)
    by_group = {g["group"]: g for g in view["groups"]}
    assert by_group["groupX"]["stream_drop"]["status"] == "drop"
    assert by_group["groupY"]["stream_drop"]["status"] == "ok"

    alerts = view["drop_alerts"]
    assert [a["group"] for a in alerts] == ["groupX"]  # only the thinned group, groupY absent
    assert alerts[0]["status"] == "drop"
    assert alerts[0]["recent"] == 40 and alerts[0]["baseline"] == 200


def test_build_coverage_timeline_caps_days(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA"])
    view = fg.build_coverage_timeline(str(root), days=10000)
    assert view["days"] == fg.TIMELINE_MAX_DAYS
    assert len(view["dates"]) == fg.TIMELINE_MAX_DAYS


def test_build_coverage_timeline_empty_store(tmp_path: Path, fake_catalog: None) -> None:
    view = fg.build_coverage_timeline(str(tmp_path / "empty"), days=5)
    assert view["anchor_date"] is None
    assert view["groups"] == []
    assert view["dates"] == []


@pytest.fixture()
def fake_oflow_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog with two REAL order-flow group names + one non-order-flow group, so the trend's
    ORDERFLOW_GROUPS filter (and its exclusion of bar/price groups) is exercised against the live constant.
    """
    rows = [
        {"feature": "tf_a", "group": "trade_flow", "version": "1.0.0", "layer": "B",
         "parity_method": "tolerance", "description": "trade flow."},
        {"feature": "sr_a", "group": "signed_trade_ratio", "version": "1.0.0", "layer": "B",
         "parity_method": "tolerance", "description": "signed ratio."},
        {"feature": "px_a", "group": "price_volume", "version": "1.0.0", "layer": "B",
         "parity_method": "tolerance", "description": "a non-order-flow bar group."},
    ]
    monkeypatch.setattr(fg, "_catalog_by_group", lambda: {
        "trade_flow": [r for r in rows if r["group"] == "trade_flow"],
        "signed_trade_ratio": [r for r in rows if r["group"] == "signed_trade_ratio"],
        "price_volume": [r for r in rows if r["group"] == "price_volume"],
    })
    monkeypatch.setattr(fg, "_group_version", lambda group: "1.0.0")


def test_orderflow_trend_union_and_intersection(tmp_path: Path, fake_oflow_catalog: None) -> None:
    root = tmp_path / "store"
    # trade_flow live: 06-17 {AAA,BBB}, 06-18 {AAA,BBB,CCC} — widening.
    _write_partition(root, "trade_flow", "stream", "2026-06-17", ["AAA", "BBB"])
    _write_partition(root, "trade_flow", "stream", "2026-06-18", ["AAA", "BBB", "CCC"])
    # signed_trade_ratio live: 06-17 {AAA}, 06-18 {AAA,BBB}.
    _write_partition(root, "signed_trade_ratio", "stream", "2026-06-17", ["AAA"])
    _write_partition(root, "signed_trade_ratio", "stream", "2026-06-18", ["AAA", "BBB"])
    # A non-order-flow group with a far WIDER live universe — must NOT inflate the order-flow trend.
    _write_partition(root, "price_volume", "stream", "2026-06-18", ["AAA", "BBB", "CCC", "DDD", "EEE"])

    view = fg.build_orderflow_coverage_trend(str(root), days=10)
    assert view["anchor_date"] == "2026-06-18"
    assert sorted(view["groups"]) == ["signed_trade_ratio", "trade_flow"]  # price_volume excluded
    by_date = {row["date"]: row for row in view["trend"]}

    # 06-18: union = {AAA,BBB,CCC} (3), intersection across both groups = {AAA,BBB} (2).
    assert by_date["2026-06-18"]["n_union"] == 3
    assert by_date["2026-06-18"]["n_intersection"] == 2
    assert by_date["2026-06-18"]["n_live_groups"] == 2
    assert by_date["2026-06-18"]["per_group"] == {"trade_flow": 3, "signed_trade_ratio": 2}
    # 06-17: union = {AAA,BBB} (2), intersection = {AAA} (1).
    assert by_date["2026-06-17"]["n_union"] == 2
    assert by_date["2026-06-17"]["n_intersection"] == 1
    # Edge-to-edge verdict: oldest captured (06-17)=2 -> newest (06-18)=3 -> widening by 1.
    assert view["oldest_captured_union"] == 2
    assert view["newest_captured_union"] == 3
    assert view["union_delta"] == 1


def test_orderflow_trend_intersection_ignores_absent_group(tmp_path: Path, fake_oflow_catalog: None) -> None:
    root = tmp_path / "store"
    # Only trade_flow captures on the anchor day; signed_trade_ratio is absent that day. The intersection
    # must be over the CAPTURING groups only (not zeroed by the absent group).
    _write_partition(root, "trade_flow", "stream", "2026-06-18", ["AAA", "BBB"])
    _write_partition(root, "signed_trade_ratio", "stream", "2026-06-17", ["AAA"])

    view = fg.build_orderflow_coverage_trend(str(root), days=5)
    by_date = {row["date"]: row for row in view["trend"]}
    assert by_date["2026-06-18"]["n_union"] == 2
    assert by_date["2026-06-18"]["n_intersection"] == 2  # only trade_flow capturing -> its own set
    assert by_date["2026-06-18"]["n_live_groups"] == 1


def test_orderflow_trend_caps_days(tmp_path: Path, fake_oflow_catalog: None) -> None:
    root = tmp_path / "store"
    _write_partition(root, "trade_flow", "stream", "2026-06-18", ["AAA"])
    view = fg.build_orderflow_coverage_trend(str(root), days=10000)
    assert view["days"] == fg.TIMELINE_MAX_DAYS
    assert len(view["dates"]) == fg.TIMELINE_MAX_DAYS


def test_orderflow_trend_empty_store(tmp_path: Path, fake_oflow_catalog: None) -> None:
    view = fg.build_orderflow_coverage_trend(str(tmp_path / "empty"), days=5)
    assert view["anchor_date"] is None
    assert view["trend"] == []
    assert view["dates"] == []


def test_trusted_names_query_uses_binary_gate() -> None:
    # The frontier's TRUSTED side keys on the binary gate (trust_state='TRUSTED'), NOT the legacy
    # lifecycle_state column the grid BADGE renders — a regression onto the legacy predicate would return an
    # empty trusted set on the live DB (0 lifecycle_state='VALIDATED' rows under the binary model).
    assert "trust_state = 'TRUSTED'" in fg._TRUSTED_NAMES_QUERY
    assert "lifecycle_state" not in fg._TRUSTED_NAMES_QUERY


def test_frontier_state_classification() -> None:
    # Already binary-trusted -> TRUSTED (a trusted feature has no open defect by construction).
    assert fg._frontier_state(True, False) == fg.FRONTIER_TRUSTED
    # An open defect blocks anything not already trusted.
    assert fg._frontier_state(False, True) == fg.FRONTIER_BLOCKED
    # No open defect + not trusted -> ELIGIBLE (the key case: a feature whose defect was cleared is one clean
    # sweep from trusted, NOT permanently red).
    assert fg._frontier_state(False, False) == fg.FRONTIER_ELIGIBLE


def test_build_trust_frontier_splits_eligible_from_blocked(
    monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    # feat_a TRUSTED; feat_b NON_TRUSTED WITH an open defect -> BLOCKED; feat_c NON_TRUSTED with NO open
    # defect (defect cleared) -> ELIGIBLE. This is the exact live shape the panel makes legible.
    monkeypatch.setattr(fg, "trusted_feature_names", lambda: {"feat_a"})
    monkeypatch.setattr(fg, "open_defect_features", lambda: {"feat_b"})

    frontier = fg.build_trust_frontier()
    assert frontier["n_features"] == 3
    assert frontier["n_trusted"] == 1
    assert frontier["n_eligible"] == 1  # feat_c: non-trusted but defect-cleared
    assert frontier["n_blocked"] == 1   # feat_b: still has an open defect
    assert frontier["n_open_defects"] == 1
    # projected = (trusted + eligible) / total = 2/3.
    assert frontier["projected_trusted_pct"] == round(100.0 * 2 / 3, 1)
    assert frontier["trusted_pct"] == round(100.0 * 1 / 3, 1)

    by_group = {g["group"]: g for g in frontier["groups"]}
    # groupX = feat_a (trusted) + feat_b (blocked); groupY = feat_c (eligible).
    assert by_group["groupX"]["n_blocked"] == 1
    assert by_group["groupX"]["blocked_features"] == ["feat_b"]
    assert by_group["groupX"]["n_trusted"] == 1
    assert by_group["groupX"]["projected_trusted_pct"] == 50.0  # 1 trusted of 2 (feat_b blocked)
    assert by_group["groupY"]["n_eligible"] == 1
    assert by_group["groupY"]["projected_trusted_pct"] == 100.0  # feat_c eligible -> projects to trusted
    # most-blocked group ranks first.
    assert frontier["groups"][0]["group"] == "groupX"


def test_build_trust_frontier_all_eligible_when_no_defects(
    monkeypatch: pytest.MonkeyPatch, fake_catalog: None
) -> None:
    # No open defects + nothing trusted yet -> everything ELIGIBLE, projecting to 100% trusted.
    monkeypatch.setattr(fg, "trusted_feature_names", lambda: set())
    monkeypatch.setattr(fg, "open_defect_features", lambda: set())
    frontier = fg.build_trust_frontier()
    assert frontier["n_trusted"] == 0
    assert frontier["n_blocked"] == 0
    assert frontier["n_eligible"] == 3
    assert frontier["projected_trusted_pct"] == 100.0


def test_build_symbol_depth_per_symbol_history(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    # AAA backfills deep (3 dates) + streams recently (1 date) -> both, full backfill depth.
    # BBB backfills only the latest date -> shallow backfill, backfill_only.
    # CCC streams only (no backfill) -> stream_only, not parity-checkable.
    _write_partition(root, "groupX", "backfill", "2025-05-12", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2025-05-13", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB"])
    _write_partition(root, "groupX", "stream", "2026-06-18", ["AAA", "CCC"])

    depth = fg.build_symbol_depth("groupX", str(root))
    assert depth["group"] == "groupX"
    assert depth["n_symbols"] == 3
    assert depth["n_both"] == 1  # AAA
    assert depth["n_backfill_only"] == 1  # BBB
    assert depth["n_stream_only"] == 1  # CCC
    assert depth["backfill_earliest"] == "2025-05-12"
    assert depth["backfill_latest"] == "2026-06-18"
    assert depth["backfill_n_dates"] == 3

    by_symbol = {row["symbol"]: row for row in depth["symbols"]}
    # AAA: full backfill depth (earliest captured date), recent stream.
    assert by_symbol["AAA"]["provenance"] == "both"
    assert by_symbol["AAA"]["backfill_earliest"] == "2025-05-12"
    assert by_symbol["AAA"]["backfill_n_dates"] == 3
    assert by_symbol["AAA"]["stream_earliest"] == "2026-06-18"
    assert by_symbol["AAA"]["stream_n_dates"] == 1
    # BBB: shallow backfill (only the latest date), no stream.
    assert by_symbol["BBB"]["provenance"] == "backfill_only"
    assert by_symbol["BBB"]["backfill_n_dates"] == 1
    assert by_symbol["BBB"]["stream_n_dates"] == 0
    assert by_symbol["BBB"]["stream_earliest"] is None
    # CCC: stream only, no settled backfill.
    assert by_symbol["CCC"]["provenance"] == "stream_only"
    assert by_symbol["CCC"]["backfill_n_dates"] == 0


def test_build_symbol_depth_span_days(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "backfill", "2026-06-15", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA"])
    depth = fg.build_symbol_depth("groupX", str(root))
    row = depth["symbols"][0]
    # inclusive calendar span 06-15..06-18 = 4 days.
    assert row["backfill_span_days"] == 4
    assert row["stream_span_days"] == 0  # no stream history


def test_build_symbol_depth_ranks_shallowest_backfill_first(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    # AAA on 2 backfill dates, BBB on 1 -> BBB (shallower) ranks first.
    _write_partition(root, "groupX", "backfill", "2026-06-17", ["AAA"])
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB"])
    depth = fg.build_symbol_depth("groupX", str(root))
    assert [row["symbol"] for row in depth["symbols"]] == ["BBB", "AAA"]


def test_build_symbol_depth_limit_caps_rows(tmp_path: Path, fake_catalog: None) -> None:
    root = tmp_path / "store"
    _write_partition(root, "groupX", "backfill", "2026-06-18", ["AAA", "BBB", "CCC", "DDD"])
    depth = fg.build_symbol_depth("groupX", str(root), limit=2)
    assert depth["n_symbols"] == 4  # summary counts ALL symbols
    assert depth["n_shown"] == 2
    assert len(depth["symbols"]) == 2  # only the rows are capped


def test_build_symbol_depth_unknown_group(fake_catalog: None) -> None:
    with pytest.raises(KeyError):
        fg.build_symbol_depth("no_such_group", "/store")
