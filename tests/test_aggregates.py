"""Tests for the parity-critical aggregation library.

The headline test is `test_live_batch_parity`: it proves that aggregating ticks
minute-by-minute with threaded state (the live path) yields byte-identical
aggregates to a single batch pass over the same ordered ticks (the backfill
path). If this ever fails, real-time and historical features have diverged.
"""
from dataclasses import asdict

from quantlib.aggregates import (
    QuoteTick,
    TickState,
    TradeTick,
    aggregate_quotes,
    aggregate_trades,
    bucket_minute,
)


def test_tick_rule_signs_volume() -> None:
    state = TickState()
    ticks = [
        TradeTick(0.0, 100.0, 10),   # no history -> default buy
        TradeTick(1.0, 101.0, 5),    # uptick -> buy
        TradeTick(2.0, 100.5, 7),    # downtick -> sell
        TradeTick(3.0, 100.5, 3),    # zero-tick -> carries sell
    ]
    agg = aggregate_trades(ticks, state)
    assert agg.buy_volume == 15.0
    assert agg.sell_volume == 10.0
    assert agg.signed_volume == 5.0
    assert agg.n_trades == 4


def test_large_print_and_percentiles() -> None:
    state = TickState()
    ticks = [TradeTick(float(i), 100.0 + i, size) for i, size in enumerate([1, 2, 3, 4, 100000])]
    agg = aggregate_trades(ticks, state, large_print_threshold=10000.0)
    assert agg.large_print_cnt == 1
    assert agg.median_size == 3.0
    assert agg.n_trades == 5


def test_empty_bucket_is_zero_not_error() -> None:
    agg = aggregate_trades([], TickState())
    assert agg.n_trades == 0 and agg.signed_volume == 0.0
    qagg = aggregate_quotes([])
    assert qagg.n_quotes == 0


def test_quote_spread_and_imbalance() -> None:
    quotes = [
        QuoteTick(0.0, bid=99.0, ask=101.0, bid_size=300, ask_size=100),
        QuoteTick(1.0, bid=100.0, ask=100.0, bid_size=100, ask_size=100),  # zero spread
    ]
    agg = aggregate_quotes(quotes)
    # first quote: spread = 2/100*1e4 = 200 bps; second: 0 bps -> mean 100
    assert abs(agg.mean_spread_bps - 100.0) < 1e-9
    # first imbalance = (300-100)/400 = 0.5; second = 0 -> mean 0.25
    assert abs(agg.quote_imbalance - 0.25) < 1e-9
    assert agg.n_quotes == 2


def _make_trade_stream() -> list[TradeTick]:
    # 3 minutes of trades with prices crossing minute boundaries, so the tick
    # rule's cross-minute state actually matters.
    prices = [100, 101, 100, 102, 103, 102, 101, 104, 100]
    ticks = []
    for i, price in enumerate(prices):
        ts = i * 25.0  # 25s spacing -> spans minutes 0,1,2
        ticks.append(TradeTick(ts, float(price), size=10 + i))
    return ticks


def test_live_batch_parity() -> None:
    ticks = _make_trade_stream()

    # Batch path: one consumer threading state across all minute buckets in order.
    batch_state = TickState()
    batch_by_minute: dict[int, list[TradeTick]] = {}
    for tick in ticks:
        batch_by_minute.setdefault(bucket_minute(tick.ts_epoch), []).append(tick)
    batch_aggs = {
        minute: aggregate_trades(batch_by_minute[minute], batch_state)
        for minute in sorted(batch_by_minute)
    }

    # Live path: minutes arrive one at a time; state persists between flushes.
    live_state = TickState()
    live_aggs = {}
    for minute in sorted(batch_by_minute):
        live_aggs[minute] = aggregate_trades(batch_by_minute[minute], live_state)

    assert {m: asdict(a) for m, a in live_aggs.items()} == {
        m: asdict(a) for m, a in batch_aggs.items()
    }
    # And the parity must hold per-minute, not just in aggregate.
    for minute in batch_aggs:
        assert asdict(live_aggs[minute]) == asdict(batch_aggs[minute])
