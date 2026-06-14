"""Sharded-capture equivalence: the multi-process executor must produce IDENTICAL features to the
single-process path. Per-symbol groups + index-replicated market context come from the sharded MAP;
the universe-wide rank comes from the gather/REDUCE. If sharding ever changed a value, parity to the
backfill would silently break — so this is the guard that lets us scale out across processes safely.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl

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
        process_bars(state, _bars_for_minute(i), "x", "mock", "2026-06-12", WINDOW, write=False)
    return state.accumulated


def _sharded(n_minutes: int) -> dict[str, pl.DataFrame]:
    shard_states = [CaptureState() for _ in range(N_SHARDS)]
    reduce_state = CaptureState()
    for i in range(n_minutes):
        bars = _bars_for_minute(i)
        for shard_id, shard_bars in enumerate(route_minute(bars, N_SHARDS)):
            if shard_bars:
                process_shard(shard_states[shard_id], shard_bars, "x", "mock", "2026-06-12", WINDOW, write=False)
        process_reduce(reduce_state, bars, "x", "mock", "2026-06-12", WINDOW, write=False)
    merged: dict[str, pl.DataFrame] = {}
    for state in shard_states:
        for name, frame in state.accumulated.items():
            merged[name] = frame if name not in merged else pl.concat([merged[name], frame]).unique(["symbol", "minute"], keep="last")
    merged.update(reduce_state.accumulated)  # the universe-wide reduce groups
    return merged


def _assert_same(single: pl.DataFrame, sharded: pl.DataFrame) -> None:
    keys = ["symbol", "minute"]
    a = single.sort(keys)
    b = sharded.sort(keys).select(a.columns)
    assert a.equals(b)


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


def test_reduce_group_absent_from_shards() -> None:
    # the universe-wide group must NOT be computed inside a shard (it would rank only the shard)
    shard_states = [CaptureState() for _ in range(N_SHARDS)]
    bars = _bars_for_minute(0)
    for shard_id, shard_bars in enumerate(route_minute(bars, N_SHARDS)):
        if shard_bars:
            process_shard(shard_states[shard_id], shard_bars, "x", "mock", "2026-06-12", WINDOW, write=False)
    assert all("cross_sectional_rank" not in state.accumulated for state in shard_states)
