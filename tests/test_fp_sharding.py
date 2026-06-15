"""Sharded-capture equivalence: the multi-process executor must produce IDENTICAL features to the
single-process path. Per-symbol groups + index-replicated market context come from the sharded MAP;
the universe-wide rank comes from the gather/REDUCE. If sharding ever changed a value, parity to the
backfill would silently break — so this is the guard that lets us scale out across processes safely.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

import polars as pl

from pathlib import Path

from quantlib.aggregates import (
    QuoteTick,
    TickState,
    TradeTick,
    aggregate_quotes,
    aggregate_trades,
    bucket_minute,
)
from quantlib.features import store
from quantlib.features.base import BatchContext
from quantlib.features.capture import CaptureState, process_bars
from quantlib.features.compare import runnable
from quantlib.features.registry import REGISTRY
from quantlib.features.sharded_capture import (
    INDEX_SYMBOLS,
    REDUCE_GROUPS,
    aggregate_shard_ticks,
    process_reduce,
    process_shard,
    route_minute,
    route_ticks,
    shard_of,
)
from quantlib.features.tick_capture import TICK_COLUMNS

BASE = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)
SYMBOLS = ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "SPY", "QQQ")
WINDOW = 300
N_SHARDS = 3
SECTORS = ("technology", "healthcare", "energy")


def _bars_for_minute(i: int) -> list[dict]:
    bars = []
    for offset, symbol in enumerate(SYMBOLS):
        close = 100.0 + offset * 3.0 + i * 0.1 + (0.5 if i % 4 == offset % 4 else -0.2)
        bars.append({"S": symbol, "o": close - 0.04, "c": close, "h": close + 0.05, "l": close - 0.05,
                     "v": 1000.0 + (i * 7 + offset) % 50, "t": (BASE + timedelta(minutes=i)).isoformat()})
    return bars


def _snapshots() -> dict[str, pl.DataFrame]:
    """The slowly-changing reference frames the gather groups read. breadth needs ``reference`` (sector) and
    ``daily`` (close) to self-select and compute its sector + 1d/5d horizons; the reader holds the FULL
    (un-sharded) snapshots so the gather sees every symbol — mirrored here for both paths."""
    reference = pl.DataFrame(
        {"symbol": list(SYMBOLS), "sector": [SECTORS[idx % len(SECTORS)] for idx in range(len(SYMBOLS))]}
    )
    daily = pl.DataFrame(
        [
            {"symbol": symbol, "date": (BASE + timedelta(days=d - 9)).date(), "close": 100.0 + off + d * 0.5}
            for off, symbol in enumerate(SYMBOLS)
            for d in range(10)
        ]
    )
    return {"reference": reference, "daily": daily}


def _single_process(n_minutes: int) -> dict[str, pl.DataFrame]:
    state = CaptureState()
    snapshots = _snapshots()
    for i in range(n_minutes):
        process_bars(state, _bars_for_minute(i), "x", "mock", "2026-06-12", WINDOW, snapshots=snapshots,
                     write=False, accumulate=True)
    return state.accumulated


def _sharded(n_minutes: int) -> dict[str, pl.DataFrame]:
    shard_states = [CaptureState() for _ in range(N_SHARDS)]
    reduce_state = CaptureState()
    snapshots = _snapshots()
    for i in range(n_minutes):
        bars = _bars_for_minute(i)
        for shard_id, shard_bars in enumerate(route_minute(bars, N_SHARDS)):
            if shard_bars:
                process_shard(shard_states[shard_id], shard_bars, "x", "mock", "2026-06-12", WINDOW,
                              snapshots=snapshots, write=False, accumulate=True)
        process_reduce(reduce_state, bars, "x", "mock", "2026-06-12", WINDOW, snapshots=snapshots,
                       write=False, accumulate=True)
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
    assert REDUCE_GROUPS == ("cross_sectional_rank", "breadth")
    _assert_same(single["cross_sectional_rank"], sharded["cross_sectional_rank"])


def test_breadth_via_reduce_identical() -> None:
    # breadth is a whole-market GATHER: per-shard it would see only ~1/N of the universe and emit N
    # different "market-wide" fractions per minute (CRITICAL-3). Routed through the reduce it must equal
    # the single-process value over ALL symbols — the live↔backfill parity the per-shard form broke.
    single, sharded = _single_process(10), _sharded(10)
    _assert_same(single["breadth"], sharded["breadth"])


def test_breadth_market_scalar_is_single_valued_per_minute() -> None:
    # The defining symptom of the per-shard bug: a "market-wide" breadth scalar took N distinct values
    # per minute (one per shard). After routing through the gather it is ONE value broadcast to every
    # ticker that minute — assert exactly one distinct market-breadth value per minute.
    sharded = _sharded(10)
    breadth = sharded["breadth"]
    per_minute = breadth.group_by("minute").agg(pl.col("breadth_net_5m").n_unique().alias("n"))
    assert per_minute.filter(pl.col("n") > 1).height == 0, "market breadth must be one scalar per minute"


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
    # the universe-wide groups must NOT be computed inside a shard (they would reduce only over the shard)
    shard_states = [CaptureState() for _ in range(N_SHARDS)]
    snapshots = _snapshots()
    bars = _bars_for_minute(0)
    for shard_id, shard_bars in enumerate(route_minute(bars, N_SHARDS)):
        if shard_bars:
            process_shard(shard_states[shard_id], shard_bars, "x", "mock", "2026-06-12", WINDOW,
                          snapshots=snapshots, write=False, accumulate=True)
    for group in REDUCE_GROUPS:
        assert all(group not in state.accumulated for state in shard_states)


# ---- Per-shard tick aggregation (the firehose distributed onto the workers) ----
# A larger universe than the old 24-symbol reader-side tick cap, so the scale-out is exercised.
TICK_SYMBOLS = tuple(f"T{i:03d}" for i in range(40)) + INDEX_SYMBOLS


def _ticks_for_minute(minute_index: int) -> tuple[list[dict], list[dict], list[dict]]:
    """One minute of bars + sub-minute trades + quotes per symbol. Prices walk so signs flip (the
    tick-rule classification is exercised, not a degenerate all-up tape)."""
    minute = BASE + timedelta(minutes=minute_index)
    bars, trades, quotes = [], [], []
    for offset, symbol in enumerate(TICK_SYMBOLS):
        close = 100.0 + offset + minute_index * 0.1 + (0.3 if (minute_index + offset) % 3 == 0 else -0.2)
        bars.append({"S": symbol, "o": close - 0.04, "c": close, "h": close + 0.05, "l": close - 0.05,
                     "v": 1000.0 + offset, "t": minute.isoformat()})
        for seq in range(5):
            ts = minute + timedelta(seconds=(seq + 1) * 10.0)
            trades.append({"S": symbol, "p": close + (seq - 2) * 0.01, "s": 100.0 + seq,
                           "ts_epoch": ts.timestamp()})
        for seq in range(4):
            ts = minute + timedelta(seconds=(seq + 1) * 12.0)
            quotes.append({"S": symbol, "bp": close - 0.02, "ap": close + 0.02,
                           "bs": 5.0 + seq, "as": 6.0 + seq, "ts_epoch": ts.timestamp()})
    return bars, trades, quotes


def _single_process_enriched(n_minutes: int) -> dict[str, dict[str, dict]]:
    """Single-process truth: ONE TickState dict, every symbol aggregated in one pass per minute."""
    states: dict[str, TickState] = {}
    out: dict[str, dict[str, dict]] = {}
    for mi in range(n_minutes):
        bars, trades, quotes = _ticks_for_minute(mi)
        minute = bars[0]["t"]
        minute_epoch = bucket_minute(datetime.fromisoformat(minute).timestamp())
        enriched, _ = aggregate_shard_ticks(bars, trades, quotes, minute_epoch, states)
        out[minute] = {bar["S"]: bar for bar in enriched}
    return out


def _sharded_enriched(n_minutes: int, n_shards: int) -> dict[str, dict[str, dict]]:
    """Sharded: each shard owns a TickState dict; bars + ticks routed by hash(symbol)."""
    shard_states: list[dict[str, TickState]] = [dict() for _ in range(n_shards)]
    out: defaultdict[str, dict[str, dict]] = defaultdict(dict)
    for mi in range(n_minutes):
        bars, trades, quotes = _ticks_for_minute(mi)
        minute = bars[0]["t"]
        minute_epoch = bucket_minute(datetime.fromisoformat(minute).timestamp())
        routed_bars = route_minute(bars, n_shards)
        routed_trades = route_ticks(trades, n_shards)
        routed_quotes = route_ticks(quotes, n_shards)
        for sid in range(n_shards):
            if not routed_bars[sid]:
                continue
            enriched, _ = aggregate_shard_ticks(routed_bars[sid], routed_trades[sid], routed_quotes[sid],
                                                minute_epoch, shard_states[sid])
            for bar in enriched:
                out[minute][bar["S"]] = bar
    return dict(out)


def test_per_shard_tick_aggregation_equals_single_process() -> None:
    """The enriched minute_agg tick columns a WORKER computes on its shard's ticks (threaded per-worker
    TickState) must equal the single-process aggregate — the tick-layer parity the scale-out preserves."""
    n_shards = 6
    single = _single_process_enriched(8)
    sharded = _sharded_enriched(8, n_shards)
    compared = 0
    for minute, single_map in single.items():
        for symbol, single_bar in single_map.items():
            sharded_bar = sharded[minute][symbol]
            for col in TICK_COLUMNS:
                compared += 1
                assert abs(single_bar[col] - sharded_bar[col]) <= 1e-9, (
                    f"{symbol} {minute} {col}: single={single_bar[col]} sharded={sharded_bar[col]}"
                )
    assert compared > 24 * len(TICK_COLUMNS)  # exercised past the old 24-symbol tick cap


def test_route_ticks_replicates_index_and_hashes_rest() -> None:
    n_shards = 6
    _, trades, _ = _ticks_for_minute(0)
    routed = route_ticks(trades, n_shards)
    # index ETF trades are replicated to EVERY shard (their bars are too -> each shard enriches its copy)
    for sid in range(n_shards):
        for index_symbol in INDEX_SYMBOLS:
            assert any(tick["S"] == index_symbol for tick in routed[sid])
    # non-index trades land ONLY on their owning shard (same hash as the bars)
    for symbol in TICK_SYMBOLS:
        if symbol in INDEX_SYMBOLS:
            continue
        owner = shard_of(symbol, n_shards)
        for sid in range(n_shards):
            present = any(tick["S"] == symbol for tick in routed[sid])
            assert present == (sid == owner)


def test_raw_trades_groups_runnable_only_with_trades_frame() -> None:
    """tick_runlength + microstructure_burst (InputSpec name='trades') are NOT runnable off the bars/
    minute_agg frame alone — they become runnable ONLY once the worker supplies the raw trades frame, and
    then they emit real (non-null) rows. This is the gap the refactor closes."""
    n_shards = 4
    bars, trades, quotes = _ticks_for_minute(0)
    minute_epoch = bucket_minute(BASE.timestamp())
    routed_bars = route_minute(bars, n_shards)
    routed_trades = route_ticks(trades, n_shards)
    routed_quotes = route_ticks(quotes, n_shards)
    enriched, trades_df = aggregate_shard_ticks(routed_bars[0], routed_trades[0], routed_quotes[0],
                                                minute_epoch, dict())
    minute_agg = pl.DataFrame(
        [{"symbol": bar["S"], "minute": datetime.fromisoformat(bar["t"]), "open": bar["o"],
          "close": bar["c"], "high": bar["h"], "low": bar["l"], "volume": bar["v"],
          **{col: bar[col] for col in TICK_COLUMNS}} for bar in enriched]
    )
    without_trades = {g.name for g in runnable({"minute_agg": minute_agg})}
    with_trades = {g.name for g in runnable({"minute_agg": minute_agg, "trades": trades_df})}
    for group_name in ("tick_runlength", "microstructure_burst"):
        assert group_name not in without_trades
        assert group_name in with_trades
        out = REGISTRY.get_group(group_name).compute(BatchContext(frames={"minute_agg": minute_agg, "trades": trades_df}))
        assert out.height > 0
        for feature in out.columns:
            if feature in ("symbol", "minute"):
                continue
            assert out.select(pl.col(feature).is_not_null().any()).item(), f"{group_name}.{feature} all-null"


def test_raw_trades_frame_sign_aggregation_matches_batch() -> None:
    """The shard's raw trades frame fed to the groups carries the SAME ordered ticks a batch pass sees, and
    the worker's threaded TickState reproduces a batch sign classification across minutes (parity)."""
    symbol = "T000"
    n_shards = 4
    shard_states: dict[str, TickState] = {}
    batch_state = TickState()
    for mi in range(5):
        bars, trades, quotes = _ticks_for_minute(mi)
        minute_epoch = bucket_minute((BASE + timedelta(minutes=mi)).timestamp())
        owner = shard_of(symbol, n_shards)
        routed_bars = route_minute(bars, n_shards)
        routed_trades = route_ticks(trades, n_shards)
        routed_quotes = route_ticks(quotes, n_shards)
        enriched, _ = aggregate_shard_ticks(routed_bars[owner], routed_trades[owner], routed_quotes[owner],
                                            minute_epoch, shard_states)
        live = {bar["S"]: bar for bar in enriched}[symbol]
        symbol_trades = [TradeTick(tick["ts_epoch"], tick["p"], tick["s"])
                         for tick in trades if tick["S"] == symbol and bucket_minute(tick["ts_epoch"]) == minute_epoch]
        symbol_quotes = [QuoteTick(tick["ts_epoch"], tick["bp"], tick["ap"], tick["bs"], tick["as"])
                         for tick in quotes if tick["S"] == symbol and bucket_minute(tick["ts_epoch"]) == minute_epoch]
        batch_trade = aggregate_trades(symbol_trades, batch_state)
        batch_quote = aggregate_quotes(symbol_quotes)
        assert live["signed_volume"] == batch_trade.signed_volume
        assert live["n_trades"] == float(batch_trade.n_trades)
        assert abs(live["mean_spread_bps"] - batch_quote.mean_spread_bps) <= 1e-9
