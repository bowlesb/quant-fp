"""Unit tests for reconcile_manifest_from_disk — the OOM-orphan recovery.

A crash can lose a worker's unflushed manifest buffer while its partitions are already on disk, so a
naive resume re-fetches those complete units. Reconcile records the orphaned on-disk partitions into the
manifest so resume skips them. These tests write real partitions to a tmp store and assert the orphans
are picked up, that already-recorded partitions are left alone, and that reconcile is idempotent.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from quantlib.data.raw_store import (done_keys, load_manifest,
                                     reconcile_manifest_from_disk,
                                     write_manifest_part, write_partition)

DAY = dt.date(2026, 6, 12)


def _frame() -> pl.DataFrame:
    return pl.DataFrame({"ts": [1, 2, 3], "price": [10.0, 11.0, 12.0]})


def test_reconcile_records_orphaned_partition(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    # Partition on disk but NOT in the manifest (the OOM-orphan case).
    write_partition(store, "trades", "AAPL", DAY, _frame())
    reconciled = reconcile_manifest_from_disk(store, "trades")
    assert reconciled == 1
    done = done_keys(load_manifest(store, "trades"))
    assert ("AAPL", DAY.isoformat()) in done


def test_reconcile_skips_already_recorded(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    write_partition(store, "trades", "AAPL", DAY, _frame())
    write_manifest_part(
        store,
        "trades",
        [
            {
                "tier": "trades",
                "symbol": "AAPL",
                "date": DAY.isoformat(),
                "rows": 3,
                "bytes": 100,
                "fetched_at": dt.datetime.now(dt.timezone.utc),
            }
        ],
        1,
    )
    assert reconcile_manifest_from_disk(store, "trades") == 0


def test_reconcile_records_orphaned_bars_partition(tmp_path: pytest.TempPathFactory) -> None:
    # bars is the tier raw_backfill.run() historically OMITTED from its reconcile loop, so a broad bars
    # deepfill that lost its manifest buffer orphaned millions of on-disk partitions the manifest never
    # recorded (observed 2026-06-19). Reconcile must record bars orphans exactly like trades/quotes.
    store = str(tmp_path)
    write_partition(store, "bars", "AAPL", DAY, _frame())
    assert reconcile_manifest_from_disk(store, "bars") == 1
    assert ("AAPL", DAY.isoformat()) in done_keys(load_manifest(store, "bars"))


def test_reconcile_is_idempotent(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    for symbol in ("AAPL", "NVDA", "MSFT"):
        write_partition(store, "trades", symbol, DAY, _frame())
    assert reconcile_manifest_from_disk(store, "trades") == 3
    assert reconcile_manifest_from_disk(store, "trades") == 0


def test_reconcile_reads_real_rows_from_disk(tmp_path: pytest.TempPathFactory) -> None:
    store = str(tmp_path)
    write_partition(store, "quotes", "SPY", DAY, _frame())
    reconcile_manifest_from_disk(store, "quotes")
    manifest = load_manifest(store, "quotes")
    row = manifest.filter(pl.col("symbol") == "SPY").row(0, named=True)
    assert row["rows"] == 3
    assert row["bytes"] > 0


def test_reconcile_symbol_scope_records_only_in_scope(
    tmp_path: pytest.TempPathFactory,
) -> None:
    # A symbol-scoped run (WINDOW+symbols / SAMPLE) can only re-fetch its own symbols, so reconcile must
    # record THEIR orphans and IGNORE every other symbol's — that scoping is the whole startup-cost win.
    store = str(tmp_path)
    write_partition(store, "quotes", "XLK", DAY, _frame())  # in scope
    write_partition(store, "quotes", "XLE", DAY, _frame())  # in scope
    write_partition(store, "quotes", "AAPL", DAY, _frame())  # OUT of scope
    reconciled = reconcile_manifest_from_disk(store, "quotes", symbols=["XLK", "XLE"])
    assert reconciled == 2
    done = done_keys(load_manifest(store, "quotes"))
    assert ("XLK", DAY.isoformat()) in done
    assert ("XLE", DAY.isoformat()) in done
    assert ("AAPL", DAY.isoformat()) not in done  # never globbed — out of scope


def test_reconcile_symbol_scope_missing_symbol_is_noop(
    tmp_path: pytest.TempPathFactory,
) -> None:
    # A fresh target with no partitions on disk matches nothing — a cheap no-op, no error.
    store = str(tmp_path)
    write_partition(store, "quotes", "AAPL", DAY, _frame())  # out of scope
    assert reconcile_manifest_from_disk(store, "quotes", symbols=["NEWSYM"]) == 0
    # The out-of-scope orphan stays unrecorded (the scope did not touch it).
    assert ("AAPL", DAY.isoformat()) not in done_keys(load_manifest(store, "quotes"))


def test_reconcile_symbols_none_scans_full_tier(
    tmp_path: pytest.TempPathFactory,
) -> None:
    # Regression: the full-universe path (symbols=None) must keep scanning the WHOLE tier (the broad-bars
    # 2026-06-19 orphan-recovery case depends on it), unchanged from the original behavior.
    store = str(tmp_path)
    for symbol in ("AAPL", "NVDA", "MSFT"):
        write_partition(store, "bars", symbol, DAY, _frame())
    assert reconcile_manifest_from_disk(store, "bars", symbols=None) == 3
    done = done_keys(load_manifest(store, "bars"))
    for symbol in ("AAPL", "NVDA", "MSFT"):
        assert (symbol, DAY.isoformat()) in done
