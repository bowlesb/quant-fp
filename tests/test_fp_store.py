"""FP0 store tests: the Parquet read API (R13) round-trips, tracks source, raises on unknown."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from quantlib.features import store
from quantlib.features.base import BatchContext
from quantlib.features.engine import run_all
from quantlib.features.registry import REGISTRY

BASE_MINUTE = datetime(2026, 6, 12, 8, 0)


def _minute_agg(n: int = 60) -> pl.DataFrame:
    rows = [
        {"symbol": symbol, "minute": BASE_MINUTE + timedelta(minutes=i), "close": 100.0 + i * 0.1}
        for symbol in ("AAA", "BBB")
        for i in range(n)
    ]
    return pl.DataFrame(rows)


def _ret1m_frame(value: float, n: int) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": ["AAA"] * n, "minute": [BASE_MINUTE + timedelta(minutes=i) for i in range(n)], "ret_1m": [value] * n}
    )


def test_store_roundtrip(tmp_path: Path) -> None:
    ctx = BatchContext(frames={"minute_agg": _minute_agg()})
    price = REGISTRY.get_group("price_returns")
    vector = run_all([price], ctx)
    store.write_group(tmp_path, price.name, price.version, "backfill", "2026-06-12", vector)

    got = store.get_features(
        ["ret_5m"], ["AAA"], BASE_MINUTE, BASE_MINUTE + timedelta(minutes=59), tmp_path
    )
    direct = (
        vector.filter(pl.col("symbol") == "AAA")
        .select(["symbol", "minute", "ret_5m"])
        .sort(["symbol", "minute"])
    )
    # Keys are exact; values round-trip through Float32 storage (intentional ~54% space narrowing), so
    # compare within Float32 precision rather than bit-exact Float64.
    assert got.select(["symbol", "minute"]).equals(direct.select(["symbol", "minute"]))
    pair = got.join(direct.rename({"ret_5m": "_d"}), on=["symbol", "minute"])
    assert pair.select(((pl.col("ret_5m") - pl.col("_d")).abs() <= 1e-6 + 1e-6 * pl.col("_d").abs()).all()).item()


def test_store_idempotent_overwrite(tmp_path: Path) -> None:
    ctx = BatchContext(frames={"minute_agg": _minute_agg()})
    price = REGISTRY.get_group("price_returns")
    vector = run_all([price], ctx)
    store.write_group(tmp_path, price.name, price.version, "backfill", "2026-06-12", vector)
    store.write_group(tmp_path, price.name, price.version, "backfill", "2026-06-12", vector)  # rerun
    got = store.get_features(["ret_1m"], "universe", BASE_MINUTE, BASE_MINUTE + timedelta(minutes=59), tmp_path)
    assert got.height == 120  # 2 symbols x 60 minutes, not doubled


def test_store_auto_prefers_backfill_then_stream(tmp_path: Path) -> None:
    store.write_group(tmp_path, "price_returns", "1.0.0", "stream", "2026-06-12", _ret1m_frame(1.0, 10))
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _ret1m_frame(2.0, 5))
    got = store.get_features(
        ["ret_1m"], "universe", BASE_MINUTE, BASE_MINUTE + timedelta(minutes=9), tmp_path
    ).sort("minute")
    values = got["ret_1m"].to_list()
    assert values[:5] == [2.0] * 5  # backfill (settled truth) wins where available
    assert values[5:] == [1.0] * 5  # stream fills the unsettled remainder


def test_store_unknown_feature_raises(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        store.get_features(["does_not_exist"], "universe", BASE_MINUTE, BASE_MINUTE, tmp_path)


def _ret1m_for(symbol: str, value: float, n: int = 5) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [symbol] * n,
            "minute": [BASE_MINUTE + timedelta(minutes=i) for i in range(n)],
            "ret_1m": [value] * n,
        }
    )


def test_sharded_backfill_chunks_union_not_clobber(tmp_path: Path) -> None:
    """The chunked-sweep regression: writing DISJOINT symbol batches as separate shards into the same
    (group, source=backfill, date) partition must UNION on read, not clobber. With the pre-fix single
    ``data.parquet`` write, the second chunk overwrote the first and only its symbols survived — the bug
    that collapsed the backfill side of every multi-chunk day to its last chunk."""
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _ret1m_for("AAA", 1.0), shard=0)
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _ret1m_for("BBB", 2.0), shard=1)
    got = store.get_features(
        ["ret_1m"], "universe", BASE_MINUTE, BASE_MINUTE + timedelta(minutes=4), tmp_path, source="backfill"
    )
    assert sorted(got["symbol"].unique().to_list()) == ["AAA", "BBB"]  # both chunks survive
    assert got.height == 10  # 2 symbols x 5 minutes, neither chunk clobbered


def test_fragmented_gather_dups_deduped_to_latest_write(tmp_path: Path) -> None:
    """The fragmented-gather poison-parity regression: when a live restart splits one minute across several
    concurrent partial gathers, the SAME (symbol, minute) lands in multiple shard files with DIVERGING
    values (each gather saw a different bar count → e.g. volume_zscore pinned to n=2 vs the real n>=3). The
    read must collapse them to the LATEST-WRITTEN file's row (last-write-wins = the most-complete gather),
    not union the duplicates (which poisoned the stream-vs-backfill parity diff for every per-symbol bar
    group). Here ``ret_1m`` stands in for any per-symbol bar feature."""
    early_minute = datetime(2026, 6, 12, 8, 0)
    early = pl.DataFrame({"symbol": ["AAA"], "minute": [early_minute], "ret_1m": [0.047]})  # partial gather (n=2)
    late = pl.DataFrame({"symbol": ["AAA"], "minute": [early_minute], "ret_1m": [13.3]})  # full gather (n>=3), correct
    early_path = store.write_group(
        tmp_path, "price_returns", "1.0.0", "stream", "2026-06-12", early, shard=0, minute=early_minute
    )
    late_path = store.write_group(
        tmp_path, "price_returns", "1.0.0", "stream", "2026-06-12", late, shard=1, minute=early_minute
    )

    # Two distinct shard files for the SAME (symbol, minute) -> the duplicate exists on disk (the poison).
    files = sorted((tmp_path / "group=price_returns/v=1.0.0/source=stream/date=2026-06-12").glob("data*.parquet"))
    assert len(files) == 2
    assert pl.read_parquet(files).height == 2  # raw union would carry both rows

    # Pin mtimes so the LATE shard is unambiguously the newest write (no wall-clock flakiness).
    os.utime(early_path, (1_750_000_000, 1_750_000_000))
    os.utime(late_path, (1_750_000_100, 1_750_000_100))

    got = store.get_features(["ret_1m"], "universe", early_minute, early_minute, tmp_path, source="stream")
    assert got.height == 1  # deduped to one row per (symbol, minute)
    # The latest-written (most-complete gather) value wins, within Float32 storage precision.
    assert got["ret_1m"].item() == pytest.approx(13.3, rel=1e-6)


def test_clean_minutes_unaffected_by_dedupe(tmp_path: Path) -> None:
    """The dedupe is a no-op on a coherent capture: every (symbol, minute) is written exactly once, so all
    rows survive untouched and values round-trip — the fix cannot silently drop legitimate rows."""
    store.write_group(tmp_path, "price_returns", "1.0.0", "stream", "2026-06-12", _ret1m_frame(1.5, 10))
    got = store.get_features(
        ["ret_1m"], "universe", BASE_MINUTE, BASE_MINUTE + timedelta(minutes=9), tmp_path, source="stream"
    ).sort("minute")
    assert got.height == 10  # all 10 minutes survive
    assert got["ret_1m"].to_list() == [1.5] * 10


def test_clear_backfill_day_removes_only_that_day_and_source(tmp_path: Path) -> None:
    """``clear_backfill_day`` deletes the day's backfill data files (all groups) so a re-sweep is a clean
    replace, while leaving the STREAM side and OTHER days untouched."""
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _ret1m_for("AAA", 1.0), shard=0)
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-12", _ret1m_for("BBB", 2.0), shard=1)
    store.write_group(tmp_path, "price_returns", "1.0.0", "stream", "2026-06-12", _ret1m_for("CCC", 3.0))
    store.write_group(tmp_path, "price_returns", "1.0.0", "backfill", "2026-06-13", _ret1m_for("DDD", 4.0))

    removed = store.clear_backfill_day(tmp_path, "2026-06-12")
    assert len(removed) == 2  # the two backfill shards for 06-12 only

    after_bf = store.get_features(
        ["ret_1m"], "universe", BASE_MINUTE, BASE_MINUTE + timedelta(minutes=4), tmp_path, source="backfill"
    )
    assert after_bf.filter(pl.col("symbol").is_in(["AAA", "BBB"]) & (pl.col("minute") < BASE_MINUTE + timedelta(days=1))).height == 0
    # stream 06-12 and backfill 06-13 are untouched
    stream = store.get_features(
        ["ret_1m"], ["CCC"], BASE_MINUTE, BASE_MINUTE + timedelta(minutes=4), tmp_path, source="stream"
    )
    assert stream.height == 5
    assert "DDD" in store.stream_symbols_on(tmp_path, "2026-06-13", source="backfill")
