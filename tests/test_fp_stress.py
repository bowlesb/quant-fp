"""Stress / resilience tests — real failure modes we'll hit at scale, not trivial unit tests.

Covers: selective repair isolation, backfill superseding stream, partial-write recovery, stale
version isolation, and the train-on-settled guard against backfill/stream mixing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from quantlib.features import store

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)


def _frame(symbol: str, value: float, n: int = 120) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": [symbol] * n, "minute": [BASE + timedelta(minutes=i) for i in range(n)], "ret_1m": [value] * n}
    )


def _read(root: Path, source: str = "auto") -> list[float]:
    got = store.get_features(["ret_1m"], "universe", BASE, BASE + timedelta(minutes=119), root, source=source)
    return got.sort("minute")["ret_1m"].to_list()


def test_repair_isolates_one_group(tmp_path: Path) -> None:
    # two groups present; re-materialize ONLY one — the other's partition must be untouched.
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _frame("AAA", 1.0))
    store.write_group(tmp_path, "trade_flow", "1.0.0", "backfill", "2026-06-12",
                      pl.DataFrame({"symbol": ["AAA"], "minute": [BASE], "trade_freq_1m": [7.0]}))
    other = next(tmp_path.glob("group=trade_flow/**/data.parquet"))
    before = other.stat().st_mtime_ns
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _frame("AAA", 9.0))  # repair
    assert other.stat().st_mtime_ns == before  # trade_flow partition untouched
    assert store.get_features(["ret_1m"], "universe", BASE, BASE, tmp_path)["ret_1m"][0] == 9.0


def test_backfill_supersedes_stream(tmp_path: Path) -> None:
    store.write_group(tmp_path, "price_returns", "1.0.0", "stream", "2026-06-12", _frame("AAA", 1.0))
    assert _read(tmp_path)[0] == 1.0  # only stream so far
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _frame("AAA", 2.0))
    assert _read(tmp_path)[0] == 2.0  # settled backfill now wins
    assert _read(tmp_path, source="stream")[0] == 1.0  # stream still retained for parity


def test_partial_write_leftover_ignored(tmp_path: Path) -> None:
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _frame("AAA", 5.0))
    # simulate a crashed write: a leftover staging dir with garbage next to the real partition
    base = tmp_path / "group=price_returns/v=1.0.0/source=backfill"
    staging = base / ".staging-date=2026-06-12"
    staging.mkdir()
    (staging / "data.parquet").write_bytes(b"corrupt-not-parquet")
    assert _read(tmp_path)[0] == 5.0  # the leftover staging dir is NOT read


def test_stale_version_not_contaminating(tmp_path: Path) -> None:
    # a leftover partition under a different version must never leak into a current-version read.
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _frame("AAA", 3.0))
    store.write_group(tmp_path, "price_returns", "9.9.9", "backfill", "2026-06-12", _frame("AAA", 99.0))
    assert _read(tmp_path)[0] == 3.0  # get_features resolves the registry version (1.0.0), not 9.9.9


def test_mock_real_storage_separation(tmp_path: Path) -> None:
    frame = pl.DataFrame({"symbol": ["AAA"], "minute": [BASE], "ret_1m": [1.0]})
    real, mock = tmp_path / "real", tmp_path / "mock"
    store.write_group(real, "price_returns", "1.0.0", "stream", "2026-06-12", frame, mode="real")
    store.write_group(mock, "price_returns", "1.0.0", "stream", "2026-06-12", frame, mode="mock")
    assert store.store_mode(real) == "real" and store.store_mode(mock) == "mock"
    # writing simulated (mock) data into the REAL store is refused — never confused
    with pytest.raises(ValueError, match="separation"):
        store.write_group(real, "price_returns", "1.0.0", "stream", "2026-06-12", frame, mode="mock")


def test_coverage_detects_capture_gap() -> None:
    from quantlib.features.compare import coverage

    minutes = [BASE + timedelta(minutes=i) for i in range(10)]  # all within ET hour 10
    backfill = pl.DataFrame({"symbol": ["AAA"] * 10, "minute": minutes, "x": [1.0] * 10})
    live = pl.DataFrame({"symbol": ["AAA"] * 9, "minute": minutes[:5] + minutes[6:], "x": [1.0] * 9})  # drop idx 5
    row = coverage(live, backfill).filter(pl.col("et_hour") == 10).row(0, named=True)
    assert row["backfill_cells"] == 10 and row["live_gaps"] == 1 and row["live_extra"] == 0
    assert row["live_coverage_pct"] == 90.0


def test_require_settled_guards_against_mixing(tmp_path: Path) -> None:
    store.write_group(tmp_path, "price_returns", "1.0.0", "stream", "2026-06-12", _frame("AAA", 1.0))
    with pytest.raises(ValueError, match="unsettled"):
        store.get_features(["ret_1m"], "universe", BASE, BASE + timedelta(minutes=119), tmp_path, require_settled=True)
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _frame("AAA", 2.0))
    ok = store.get_features(["ret_1m"], "universe", BASE, BASE + timedelta(minutes=119), tmp_path, require_settled=True)
    assert ok.sort("minute")["ret_1m"][0] == 2.0  # now settled -> allowed, returns backfill
