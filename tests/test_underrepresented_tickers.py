"""Unit tests for the universe-wide under-represented-ticker flag (ops/underrepresented_tickers).

The disk-touching ``PartitionStoreReader`` (reused from ticker_coverage) is covered by that module's tests;
the LOGIC here — one universe pass accumulating per-symbol stream/backfill group membership over a bounded
recent slice, then the live-gap roll-up (backfill − stream), the ranking, and the per-group leak tally — is
pure over the ``StoreReader`` protocol and is pinned with an IN-MEMORY fake store (no /store, no DB):

  * a symbol settled in backfill but absent from the stream is flagged (under_rep_score = #missing groups);
  * a symbol streaming everywhere it is backfilled has NO gap (fully_streamed);
  * a symbol only in the stream (never backfilled) is not flagged (nothing settled-but-missing);
  * stream presence is read only within the recent window; backfill only over the recent settled dates;
  * ranking is most-under-streamed first; the per-group tally counts how many symbols each group leaks;
  * build_report assembles the whole JSON-able shape with the right totals.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

import underrepresented_tickers as ur  # noqa: E402  (path inserted above)


class FakeStoreReader:
    """An in-memory StoreReader. ``data`` is {group: {"version", source: {date: {symbols}}}}."""

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
        return []  # unused by this report


def _group(
    version: str = "1.0.0",
    stream: dict[str, set[str]] | None = None,
    backfill: dict[str, set[str]] | None = None,
) -> dict:
    entry: dict = {"version": version}
    if stream is not None:
        entry["stream"] = stream
    if backfill is not None:
        entry["backfill"] = backfill
    return entry


RECENT = "2026-06-24"  # inside the default 7-day stream window
OLD = "2019-01-02"  # outside it


def test_backfill_present_stream_absent_is_flagged() -> None:
    reader = FakeStoreReader(
        {"alpha": _group(backfill={RECENT: {"AAPL", "MSFT"}}, stream={RECENT: {"MSFT"}})}
    )
    gaps = ur.build_symbol_gaps(reader)
    assert gaps["AAPL"].under_rep_groups == {"alpha"}  # backfilled, not streaming
    assert gaps["AAPL"].under_rep_score == 1
    assert gaps["AAPL"].fully_streamed is False
    # MSFT streams where it is backfilled -> no gap.
    assert gaps["MSFT"].fully_streamed is True
    assert gaps["MSFT"].under_rep_score == 0


def test_symbol_only_in_stream_is_not_flagged() -> None:
    # Streaming but never backfilled -> nothing settled-but-missing, so no gap.
    reader = FakeStoreReader({"alpha": _group(stream={RECENT: {"AAPL"}})})
    gaps = ur.build_symbol_gaps(reader)
    assert gaps["AAPL"].under_rep_score == 0
    assert gaps["AAPL"].backfill_groups == set()


def test_gap_accumulates_across_groups() -> None:
    reader = FakeStoreReader(
        {
            "alpha": _group(backfill={RECENT: {"AAPL"}}),  # backfill-only
            "beta": _group(backfill={RECENT: {"AAPL"}}, stream={RECENT: {"AAPL"}}),  # both
            "gamma": _group(backfill={RECENT: {"AAPL"}}),  # backfill-only
        }
    )
    gaps = ur.build_symbol_gaps(reader)
    assert gaps["AAPL"].under_rep_groups == {"alpha", "gamma"}
    assert gaps["AAPL"].under_rep_score == 2


def test_stream_presence_only_counts_within_window() -> None:
    # AAPL streams only on an OLD date outside the window -> NOT counted live -> flagged against backfill.
    reader = FakeStoreReader(
        {"alpha": _group(backfill={RECENT: {"AAPL"}}, stream={OLD: {"AAPL"}, RECENT: {"MSFT"}})}
    )
    gaps = ur.build_symbol_gaps(reader)
    assert gaps["AAPL"].under_rep_score == 1


def test_backfill_dates_bound_limits_settled_read() -> None:
    # AAPL appears only on an OLD settled date; with backfill_dates=1 only the LATEST settled date is read,
    # so AAPL is not seen as settled and is not flagged. (Bounds the present-day settled read.)
    reader = FakeStoreReader(
        {"alpha": _group(backfill={OLD: {"AAPL"}, RECENT: {"MSFT"}}, stream={RECENT: {"MSFT"}})}
    )
    gaps = ur.build_symbol_gaps(reader, backfill_dates=1)
    assert "AAPL" not in gaps or gaps["AAPL"].under_rep_score == 0
    # With a wider settled read AAPL's old settled day is seen and it is flagged.
    gaps_wide = ur.build_symbol_gaps(reader, backfill_dates=5)
    assert gaps_wide["AAPL"].under_rep_score == 1


def test_group_with_no_version_is_skipped() -> None:
    reader = FakeStoreReader({"alpha": {"version": None}})
    assert ur.build_symbol_gaps(reader) == {}


def test_rank_orders_by_score_then_symbol() -> None:
    reader = FakeStoreReader(
        {
            "alpha": _group(backfill={RECENT: {"AAA", "BBB", "CCC"}}),
            "beta": _group(backfill={RECENT: {"AAA", "BBB"}}),
            "gamma": _group(backfill={RECENT: {"AAA"}}),
        }
    )
    gaps = ur.build_symbol_gaps(reader)
    ranked = ur.rank_under_represented(gaps)
    # AAA in 3 groups (score 3), BBB in 2 (score 2), CCC in 1 (score 1).
    assert [gap.symbol for gap in ranked] == ["AAA", "BBB", "CCC"]
    assert [gap.under_rep_score for gap in ranked] == [3, 2, 1]


def test_per_group_tally_counts_leaked_symbols() -> None:
    reader = FakeStoreReader(
        {
            "alpha": _group(backfill={RECENT: {"AAA", "BBB"}}),  # both leak (no stream)
            "beta": _group(backfill={RECENT: {"AAA"}}, stream={RECENT: {"AAA"}}),  # streamed -> no leak
        }
    )
    gaps = ur.build_symbol_gaps(reader)
    tally = ur.per_group_gap_tally(gaps)
    assert tally == {"alpha": 2}  # beta leaks nothing


def test_build_report_shape_and_totals() -> None:
    reader = FakeStoreReader(
        {
            "alpha": _group(backfill={RECENT: {"AAA", "BBB"}}, stream={RECENT: {"BBB"}}),
            "beta": _group(backfill={RECENT: {"AAA"}}),
        }
    )
    report = ur.build_report(reader)
    assert report["n_symbols_seen"] == 2  # AAA, BBB
    assert report["n_symbols_backfilled"] == 2
    assert report["n_symbols_streamed"] == 1  # only BBB streams
    # AAA is under-represented (alpha + beta backfill, no stream); BBB streams where backfilled.
    assert report["n_symbols_under_represented"] == 1
    under = report["under_represented"]
    assert isinstance(under, list)
    assert under[0]["symbol"] == "AAA"
    assert under[0]["under_rep_score"] == 2
    assert report["per_group_gap"] == {"alpha": 1, "beta": 1}


def test_render_text_mentions_key_sections() -> None:
    reader = FakeStoreReader({"alpha": _group(backfill={RECENT: {"AAA"}})})
    report = ur.build_report(reader)
    text = ur.render_text(report)
    assert "UNDER-REPRESENTED TICKERS" in text
    assert "GROUPS LEAKING THE MOST SYMBOLS" in text
    assert "TOP UNDER-STREAMED SYMBOLS" in text
    assert "AAA" in text


def test_empty_store_is_clean_report() -> None:
    reader = FakeStoreReader({})
    report = ur.build_report(reader)
    assert report["n_symbols_seen"] == 0
    assert report["n_symbols_under_represented"] == 0
    assert report["under_represented"] == []
    assert report["per_group_gap"] == {}
