"""Lifecycle simulations (scenarios A & C): abandoned partial backfill, delete, retire, restore-log.

Treats the feature/iteration data lifecycle as a form of stress testing: a modeller abandons a
30%-done backfill (verify what's there → resume the rest, OR delete it entirely), and disk
reclamation (retire old dates / drop a low-value feature), all with a logged restore recipe.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from quantlib.features import lifecycle, store

BASE = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
DATES_10 = [f"2026-06-{d:02d}" for d in range(1, 11)]


def _write(root: Path, group: str, source: str, day: str) -> None:
    store.write_group(root, group, "1.0.0", source, day, pl.DataFrame({"symbol": ["AAA"], "minute": [BASE], "ret_1m": [1.0]}))


def test_A_abandoned_partial_backfill_verify_then_resume(tmp_path: Path) -> None:
    # a modeller backfilled only 3 of 10 intended dates, then abandoned the job
    for day in DATES_10[:3]:
        _write(tmp_path, "price_returns", "backfill", day)
    status = lifecycle.completeness(tmp_path, "price_returns", "1.0.0", "backfill", DATES_10)
    assert status["present"] == 3 and status["pct"] == 30.0 and len(status["missing"]) == 7
    # another agent verifies + RESUMES only the missing dates (no redo of the 30% already done)
    for day in status["missing"]:
        _write(tmp_path, "price_returns", "backfill", day)
    assert lifecycle.completeness(tmp_path, "price_returns", "1.0.0", "backfill", DATES_10)["pct"] == 100.0


def test_A_delete_entire_feature_logs_restore(tmp_path: Path) -> None:
    for day in DATES_10[:3]:
        _write(tmp_path, "price_returns", "backfill", day)
        _write(tmp_path, "price_returns", "stream", day)
    result = lifecycle.delete_feature_group(tmp_path, "price_returns")
    assert result["partitions"] == 6  # 3 dates x 2 sources, all gone
    assert not list(tmp_path.glob("group=price_returns/**/data.parquet"))
    log = (tmp_path / lifecycle.RETIREMENT_LOG).read_text()
    assert "restore" in log and "price_returns" in log  # restore recipe captured


def test_C_retire_old_dates_frees_disk_and_logs_restore(tmp_path: Path) -> None:
    for day in DATES_10:
        _write(tmp_path, "price_returns", "backfill", day)
    result = lifecycle.retire_before(tmp_path, "2026-06-05")  # wipe 06-01..06-04
    assert result["partitions"] == 4 and result["mb_freed"] >= 0
    assert lifecycle.completeness(tmp_path, "price_returns", "1.0.0", "backfill", DATES_10)["present"] == 6
    assert "re-backfill" in (tmp_path / lifecycle.RETIREMENT_LOG).read_text()


def test_C_store_status_accounting(tmp_path: Path) -> None:
    for day in DATES_10[:2]:
        _write(tmp_path, "price_returns", "backfill", day)
    _write(tmp_path, "volatility", "backfill", "2026-06-01")
    status = lifecycle.store_status(tmp_path)
    price = status.filter((pl.col("group") == "price_returns") & (pl.col("source") == "backfill")).row(0, named=True)
    assert price["dates"] == 2 and price["mb"] >= 0.0
    assert set(status["group"].to_list()) == {"price_returns", "volatility"}
