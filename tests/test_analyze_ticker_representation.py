"""Unit tests for the read-only ticker-representation analysis (ops/analyze_ticker_representation.py).

Pure helpers are tested directly; ``build_report`` is exercised end-to-end against a tiny fake store written
into a tmp dir, so the stream-vs-backfill classification, history-depth, and ranked lists are verified
against a controlled fixture (no live store touch)."""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))

import analyze_ticker_representation as atr  # noqa: E402  (path inserted above)


def _write_partition(store: Path, group: str, source: str, date_iso: str, symbols: list[str]) -> None:
    partition = store / f"group={group}" / "v=1.0.0" / f"source={source}" / f"date={date_iso}"
    partition.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols, "value": [1.0] * len(symbols)}).write_parquet(
        partition / "data.parquet"
    )


def test_sample_files_bounds_and_keeps_edges() -> None:
    files = [Path(f"data-{i}.parquet") for i in range(100)]
    sampled = atr._sample_files(files, atr.MAX_FILES_PER_PARTITION)
    assert len(sampled) <= atr.MAX_FILES_PER_PARTITION
    assert sampled[0] == files[0] and sampled[-1] == files[-1]
    assert atr._sample_files(files[:5], 12) == files[:5]  # fewer than cap -> all


def test_sample_depth_dates_keeps_edges_when_sampling() -> None:
    dates = [f"2025-{m:02d}-{d:02d}" for m in range(1, 7) for d in range(1, 11)]  # 60 dates
    sampled = atr.sample_depth_dates(dates)
    assert dates[0] in sampled and dates[-1] in sampled
    assert len(sampled) < len(dates)
    short = ["2026-06-17", "2026-06-18"]
    assert atr.sample_depth_dates(short) == short  # below threshold -> unchanged


def test_symbol_repr_under_rep_and_span() -> None:
    record = atr.SymbolRepr(symbol="AAPL")
    record.backfill_groups = {"volume", "trade_flow", "price"}
    record.stream_groups = {"volume", "price"}
    assert record.under_rep_groups == {"trade_flow"}
    record.earliest_backfill_date = "2025-01-02"
    record.latest_backfill_date = "2026-06-18"
    assert record.backfill_span_days == (atr.dt.date(2026, 6, 18) - atr.dt.date(2025, 1, 2)).days


def test_depth_distribution_bands() -> None:
    records = [
        atr.SymbolRepr(symbol="A", earliest_backfill_date="2026-06-12", latest_backfill_date="2026-06-18"),
        atr.SymbolRepr(symbol="B", earliest_backfill_date="2024-01-02", latest_backfill_date="2026-06-18"),
    ]
    dist = {row["band"]: row["n_symbols"] for row in atr.depth_distribution(records)}
    assert dist["<=7d"] == 1  # 6-day span
    assert dist[">365d"] == 1  # multi-year span


def test_build_report_classifies_under_rep_and_stream_only(tmp_path: Path) -> None:
    store = tmp_path / "store"
    # CAT: backfill in both groups, stream only in 'volume' -> under-rep in 'trade_flow'.
    _write_partition(store, "volume", "backfill", "2026-06-18", ["CAT", "BKNG"])
    _write_partition(store, "trade_flow", "backfill", "2026-06-18", ["CAT", "BKNG"])
    _write_partition(store, "volume", "stream", "2026-06-18", ["CAT", "BKNG"])
    _write_partition(store, "trade_flow", "stream", "2026-06-18", [])  # FP_TICK gap: nothing live
    # NEWLY: stream-only, never backfilled.
    _write_partition(store, "volume", "stream", "2026-06-18", ["CAT", "BKNG", "NEWLY"])
    # Give CAT deep history in 'volume'.
    _write_partition(store, "volume", "backfill", "2024-01-02", ["CAT"])

    report = atr.build_report(str(store), anchor_window_days=10, top_n=10)

    assert report["anchor_date"] == "2026-06-18"
    assert report["n_groups"] == 2
    under_rep = {row["symbol"]: row for row in report["most_under_rep_live"]}
    assert "CAT" in under_rep and under_rep["CAT"]["under_rep_groups"] == 1
    assert "BKNG" in under_rep
    assert "NEWLY" in report["stream_only_symbols"]
    assert report["n_symbols_stream_only"] >= 1

    cat = next(row for row in report["shallowest_history"] if row["symbol"] == "CAT")
    assert cat["earliest_backfill_date"] == "2024-01-02"  # earliest sampled date, not the recent one
