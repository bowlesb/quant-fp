"""Sharded-capture equivalence: the multi-process executor must produce IDENTICAL features to the
single-process path. Per-symbol groups + index-replicated market context come from the sharded MAP;
the universe-wide rank comes from the gather/REDUCE. If sharding ever changed a value, parity to the
backfill would silently break — so this is the guard that lets us scale out across processes safely.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

from pathlib import Path

from quantlib.features import store
from quantlib.features.capture import CaptureState, process_bars
from quantlib.features.sharded_capture import REDUCE_GROUPS, process_reduce, process_shard, route_minute

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "SPY", "QQQ")
WINDOW = 300
N_SHARDS = 3


def _bars_for_minute(i: int) -> list[dict]:
    bars = []
    for offset, symbol in enumerate(SYMBOLS):
        close = 100.0 + offset * 3.0 + i * 0.1 + (0.5 if i % 4 == offset % 4 else -0.2)
        bars.append({"S": symbol, "o": close - 0.04, "c": close, "h": close + 0.05, "l": close - 0.05,
                     "v": 1000.0 + (i * 7 + offset) % 50, "t": (BASE + timedelta(minutes=i)).isoformat()})
    return bars


def _single_process(n_minutes: int) -> dict[str, pl.DataFrame]:
    state = CaptureState()
    for i in range(n_minutes):
        process_bars(state, _bars_for_minute(i), "x", "mock", "2026-06-12", WINDOW, write=False, accumulate=True)
    return state.accumulated


def _sharded(n_minutes: int) -> dict[str, pl.DataFrame]:
    shard_states = [CaptureState() for _ in range(N_SHARDS)]
    reduce_state = CaptureState()
    for i in range(n_minutes):
        bars = _bars_for_minute(i)
        for shard_id, shard_bars in enumerate(route_minute(bars, N_SHARDS)):
            if shard_bars:
                process_shard(shard_states[shard_id], shard_bars, "x", "mock", "2026-06-12", WINDOW, write=False, accumulate=True)
        process_reduce(reduce_state, bars, "x", "mock", "2026-06-12", WINDOW, write=False, accumulate=True)
    merged: dict[str, pl.DataFrame] = {}
    for state in shard_states:
        for name, frame in state.accumulated.items():
            merged[name] = frame if name not in merged else pl.concat([merged[name], frame]).unique(["symbol", "minute"], keep="last")
    merged.update(reduce_state.accumulated)  # the universe-wide reduce groups
    return merged


def _assert_same(single: pl.DataFrame, sharded: pl.DataFrame) -> None:
    # Sharding must preserve values within the PARITY tolerance (1e-9), the same standard as
    # live-vs-backfill — NOT bit-exactness: aggregate-at-T (group_by mean/std in compute_latest) is
    # float-order-sensitive, and a shard's buffer orders rows differently than the single-process one.
    keys = ["symbol", "minute"]
    a = single.sort(keys)
    b = sharded.sort(keys).select(a.columns)
    assert a.height == b.height
    for feature in [c for c in a.columns if c not in keys]:
        pair = a.select("symbol", "minute", feature).join(
            b.select("symbol", "minute", pl.col(feature).alias("_b")), on=keys
        )
        bad = pair.filter(
            ~(
                (pl.col(feature).is_null() & pl.col("_b").is_null())
                | ((pl.col(feature) - pl.col("_b")).abs() <= 1e-9 + 1e-9 * pl.col(feature).abs())
            )
        )
        assert bad.height == 0, f"{feature}: sharded != single on {bad.height} cells"


def test_sharded_per_symbol_groups_identical() -> None:
    single, sharded = _single_process(10), _sharded(10)
    for group in ("price_returns", "trend_quality", "volume", "candlestick"):
        _assert_same(single[group], sharded[group])


def test_sharded_market_context_identical_via_index_replication() -> None:
    single, sharded = _single_process(10), _sharded(10)
    # SPY/QQQ replicated into every shard, so the broadcast market returns must match single-process
    for group in ("market_context", "market_beta"):
        _assert_same(single[group], sharded[group])


def test_cross_sectional_rank_via_reduce_identical() -> None:
    single, sharded = _single_process(10), _sharded(10)
    assert REDUCE_GROUPS == ("cross_sectional_rank",)
    _assert_same(single["cross_sectional_rank"], sharded["cross_sectional_rank"])


def test_store_concurrent_shard_writes_do_not_clobber(tmp_path: Path) -> None:
    # the #6 concurrency fix: N shards write disjoint symbols to the SAME (group, date) partition;
    # each writes its own data-<shard>.parquet, so the read returns the UNION (no last-writer-wins).
    root = str(tmp_path / "store")
    day = "2026-06-12"
    for shard, symbol, ret in ((0, "AAA", 0.01), (1, "BBB", 0.02), (2, "CCC", 0.03)):
        frame = pl.DataFrame({"symbol": [symbol], "minute": [BASE], "ret_1m": [ret]})
        store.write_group(root, "price_returns", "1.0.0", "stream", day, frame, mode="mock", shard=shard)
    df = store.get_features(["ret_1m"], "universe", BASE, BASE + timedelta(minutes=5), root, source="stream")
    assert set(df["symbol"].to_list()) == {"AAA", "BBB", "CCC"}  # all 3 shards present, none clobbered


def test_reduce_group_absent_from_shards() -> None:
    # the universe-wide group must NOT be computed inside a shard (it would rank only the shard)
    shard_states = [CaptureState() for _ in range(N_SHARDS)]
    bars = _bars_for_minute(0)
    for shard_id, shard_bars in enumerate(route_minute(bars, N_SHARDS)):
        if shard_bars:
            process_shard(shard_states[shard_id], shard_bars, "x", "mock", "2026-06-12", WINDOW, write=False, accumulate=True)
    assert all("cross_sectional_rank" not in state.accumulated for state in shard_states)
