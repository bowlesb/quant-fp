"""Unit tests for the per-ticker coverage report (ops/ticker_coverage).

The disk-touching ``PartitionStoreReader`` is thin (directory globs + bounded parquet reads); the LOGIC —
walking groups for one symbol, deciding stream/backfill/both presence, bounding history reach, rolling up
breadth / the live gap / shallowest history, and joining the trust table — is pure over the ``StoreReader``
protocol and a trust map, and is what these tests pin with an IN-MEMORY fake store (no /store, no DB):

  * a symbol present only in a group's stream window is in_stream, not in_backfill;
  * a symbol present only in backfill is in_backfill with earliest/latest from the SAMPLED present dates;
  * a symbol in both is stream+backfill; a symbol in neither is not covered;
  * backfill_only (the per-ticker FP_TICK_SYMBOLS live gap) = backfill-present AND stream-absent;
  * covered_features flattens the covered groups' feature columns; uncovered groups contribute none;
  * summarize_trust tallies TRUSTED / DIVERGENT / untracked over the covered features;
  * build_report assembles the whole JSON-able shape, with/without the trust join;
  * sample_history_dates keeps the edges (so the earliest-reach bound stays tight) and bounds the count.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

import ticker_coverage as tc  # noqa: E402  (path inserted above)


class FakeStoreReader:
    """An in-memory StoreReader. ``data`` is {group: {"version", "features", source: {date: {symbols}}}}."""

    def __init__(self, data: dict[str, dict]) -> None:
        self.data = data

    def list_groups(self) -> list[str]:
        return sorted(self.data)

    def group_version(self, group: str) -> str | None:
        entry = self.data.get(group)
        return entry["version"] if entry else None

    def partition_dates(self, group: str, version: str, source: str) -> list[str]:
        return sorted(self.data[group].get(source, {}))

    def symbols_on_date(self, group: str, version: str, source: str, date_iso: str) -> set[str]:
        return set(self.data[group].get(source, {}).get(date_iso, set()))

    def group_features(self, group: str, version: str) -> list[str]:
        return list(self.data[group].get("features", []))


def _group(
    version: str = "1.0.0",
    features: list[str] | None = None,
    stream: dict[str, set[str]] | None = None,
    backfill: dict[str, set[str]] | None = None,
) -> dict:
    entry: dict = {"version": version, "features": features or []}
    if stream is not None:
        entry["stream"] = stream
    if backfill is not None:
        entry["backfill"] = backfill
    return entry


# A recent date inside the stream window and an old one outside it, so stream-window logic is exercised.
RECENT = "2026-06-24"
OLD = "2019-01-02"


def test_stream_only_is_in_stream_not_backfill() -> None:
    reader = FakeStoreReader({"alpha": _group(features=["a1"], stream={RECENT: {"AAPL"}})})
    coverage = tc.build_group_coverage(reader, "AAPL", "alpha")
    assert coverage is not None
    assert coverage.in_stream is True
    assert coverage.in_backfill is False
    assert coverage.covered is True
    assert coverage.backfill_only is False
    assert coverage.features == ["a1"]


def test_backfill_only_sets_history_and_live_gap() -> None:
    reader = FakeStoreReader(
        {"alpha": _group(features=["a1", "a2"], backfill={OLD: {"AAPL"}, RECENT: {"AAPL"}})}
    )
    coverage = tc.build_group_coverage(reader, "AAPL", "alpha")
    assert coverage is not None
    assert coverage.in_stream is False
    assert coverage.in_backfill is True
    assert coverage.backfill_only is True  # the per-ticker live gap
    assert coverage.earliest_backfill_date == OLD
    assert coverage.latest_backfill_date == RECENT
    assert coverage.backfill_span_days > 2000


def test_both_sources_is_stream_plus_backfill() -> None:
    reader = FakeStoreReader(
        {"alpha": _group(features=["a1"], stream={RECENT: {"AAPL"}}, backfill={OLD: {"AAPL"}})}
    )
    coverage = tc.build_group_coverage(reader, "AAPL", "alpha")
    assert coverage is not None
    assert coverage.in_stream and coverage.in_backfill
    assert coverage.backfill_only is False


def test_absent_symbol_is_not_covered_and_has_no_features() -> None:
    reader = FakeStoreReader({"alpha": _group(features=["a1"], backfill={RECENT: {"MSFT"}})})
    coverage = tc.build_group_coverage(reader, "AAPL", "alpha")
    assert coverage is not None
    assert coverage.covered is False
    # An uncovered group reports no features (the symbol does not have them).
    assert coverage.features == []


def test_group_with_no_version_is_skipped() -> None:
    reader = FakeStoreReader({"alpha": {"version": None, "features": []}})
    assert tc.build_group_coverage(reader, "AAPL", "alpha") is None
    # And the whole-ticker walk drops it.
    assert tc.build_ticker_coverage(reader, "AAPL") == []


def test_stream_presence_only_counts_within_window() -> None:
    # Present in stream only on an OLD date outside the window -> NOT counted as live stream presence.
    reader = FakeStoreReader({"alpha": _group(features=["a1"], stream={OLD: {"AAPL"}, RECENT: {"MSFT"}})})
    coverage = tc.build_group_coverage(reader, "AAPL", "alpha")
    assert coverage is not None
    assert coverage.in_stream is False


def test_build_ticker_coverage_orders_covered_first() -> None:
    reader = FakeStoreReader(
        {
            "z_covered": _group(features=["z1"], backfill={RECENT: {"AAPL"}}),
            "a_uncovered": _group(features=["a1"], backfill={RECENT: {"MSFT"}}),
        }
    )
    coverages = tc.build_ticker_coverage(reader, "AAPL")
    assert [item.group for item in coverages] == ["z_covered", "a_uncovered"]
    assert coverages[0].covered is True
    assert coverages[1].covered is False


def test_covered_features_flattens_only_covered_groups() -> None:
    reader = FakeStoreReader(
        {
            "alpha": _group(features=["a1", "a2"], backfill={RECENT: {"AAPL"}}),
            "beta": _group(features=["b1"], stream={RECENT: {"AAPL"}}),
            "gamma": _group(features=["g1"], backfill={RECENT: {"MSFT"}}),  # AAPL absent
        }
    )
    coverages = tc.build_ticker_coverage(reader, "AAPL")
    assert tc.covered_features(coverages) == ["a1", "a2", "b1"]  # gamma's g1 excluded


def test_summarize_trust_tallies_states() -> None:
    trust = {
        "a1": tc.FeatureTrust("a1", "TRUSTED", "VALIDATED"),
        "a2": tc.FeatureTrust("a2", "NON_TRUSTED", "DIVERGENT"),
        # b1 has no trust row -> untracked
    }
    tally = tc.summarize_trust(["a1", "a2", "b1"], trust)
    assert tally == {"total": 3, "trusted": 1, "divergent": 1, "untracked": 1}


def test_build_report_without_trust() -> None:
    reader = FakeStoreReader(
        {
            "alpha": _group(features=["a1", "a2"], backfill={OLD: {"AAPL"}, RECENT: {"AAPL"}}),
            "beta": _group(features=["b1"], stream={RECENT: {"AAPL"}}, backfill={RECENT: {"AAPL"}}),
            "gamma": _group(features=["g1"], backfill={RECENT: {"MSFT"}}),  # AAPL absent
        }
    )
    report = tc.build_report(reader, "AAPL")
    assert report["symbol"] == "AAPL"
    assert report["n_groups_total"] == 3
    assert report["n_groups_covered"] == 2
    # alpha is backfill-only (live gap); beta is stream+backfill.
    assert report["n_groups_backfill_only"] == 1
    assert report["backfill_only_groups"] == ["alpha"]
    assert report["n_features_covered"] == 3
    assert "trust" not in report  # no trust map supplied


def test_build_report_with_trust_join() -> None:
    reader = FakeStoreReader({"alpha": _group(features=["a1", "a2"], backfill={RECENT: {"AAPL"}})})
    trust = {
        "a1": tc.FeatureTrust("a1", "TRUSTED", None),
        "a2": tc.FeatureTrust("a2", "NON_TRUSTED", "DIVERGENT"),
    }
    report = tc.build_report(reader, "AAPL", trust)
    assert report["trust"] == {"total": 2, "trusted": 1, "divergent": 1, "untracked": 0}
    feature_trust = report["feature_trust"]
    assert isinstance(feature_trust, list)
    by_name = {row["feature"]: row for row in feature_trust}
    assert by_name["a1"]["trust_state"] == "TRUSTED"
    assert by_name["a2"]["lifecycle_state"] == "DIVERGENT"


def test_sample_history_dates_keeps_edges_and_bounds_count() -> None:
    dates = [f"2020-01-{day:02d}" for day in range(1, 29)]  # 28 dates
    sampled = tc.sample_history_dates(dates)
    # Edges are kept so the earliest/latest reach bound is as tight as the data allows.
    assert dates[0] in sampled and dates[-1] in sampled
    # Bounded well under the input size.
    assert len(sampled) <= 2 * tc.HISTORY_EDGE_DATES + tc.HISTORY_INTERIOR_DATES
    # A short date list is returned whole (no sampling loss).
    short = ["2020-01-01", "2020-01-02"]
    assert tc.sample_history_dates(short) == short


def test_render_text_is_stable_and_mentions_key_rollups() -> None:
    reader = FakeStoreReader({"alpha": _group(features=["a1"], backfill={OLD: {"AAPL"}, RECENT: {"AAPL"}})})
    report = tc.build_report(reader, "AAPL")
    text = tc.render_text(report)
    assert "FEATURE-STORE COVERAGE — AAPL" in text
    assert "LIVE GAP" in text  # alpha is backfill-only
    assert "alpha" in text
