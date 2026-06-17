"""Tests for the raw market-data (md:) channel: codec round-trips for the bar + tick frames, the
zero-copy tick payload view, magic/shape validation, and a synthetic publish->consume round-trip that
mirrors the fv: transport test (skips cleanly when Redis is unreachable). The synthetic path proves a
container/researcher reads back COMPLETE raw records over the per-tier per-symbol streams without a live
producer or market hours.
"""
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pytest
import redis

from quantlib.bus.market_data import (
    BAR_STREAM,
    QUOTE_COLS,
    TICK_TRADES_STREAM,
    TRADE_COLS,
    TRADES_STREAM,
    BarRecord,
    MarketDataConsumer,
    MarketDataPublisher,
    TickBatch,
    decode_bar,
    decode_ticks,
    encode_bar,
    encode_tick_rows,
    encode_ticks,
    md_stream_key,
)

URL = os.environ.get("BUS_REDIS_URL", "redis://quant-redis:6379/0")
MINUTE = dt.datetime(2026, 6, 15, 14, 31, tzinfo=dt.timezone.utc)

TRADES = [
    {"S": "AAPL", "p": 190.25, "s": 100.0, "ts_epoch": MINUTE.timestamp() + 1.5},
    {"S": "AAPL", "p": 190.30, "s": 50.0, "ts_epoch": MINUTE.timestamp() + 2.0},
]
QUOTES = [
    {"S": "AAPL", "bp": 190.20, "ap": 190.30, "bs": 300.0, "as": 200.0, "ts_epoch": MINUTE.timestamp() + 1.1},
]


def _redis_up() -> bool:
    try:
        redis.Redis.from_url(URL).ping()
        return True
    except (redis.exceptions.RedisError, OSError):
        return False


def test_bar_round_trip() -> None:
    frame = encode_bar("AAPL", MINUTE, (1.0, 3.0, 0.5, 2.0, 1000.0))
    bar = decode_bar(frame)
    assert isinstance(bar, BarRecord)
    assert bar.symbol == "AAPL"
    assert bar.minute == MINUTE
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (1.0, 3.0, 0.5, 2.0, 1000.0)


def test_bar_bad_magic_raises() -> None:
    frame = bytearray(encode_bar("AAPL", MINUTE, (1.0, 2.0, 0.5, 1.5, 10.0)))
    frame[0:4] = b"XXXX"
    with pytest.raises(ValueError, match="bad magic"):
        decode_bar(bytes(frame))


def test_trades_round_trip() -> None:
    batch = decode_ticks(encode_ticks("AAPL", MINUTE, "trades", TRADES))
    assert isinstance(batch, TickBatch)
    assert batch.symbol == "AAPL"
    assert batch.minute == MINUTE
    assert batch.kind == "trades"
    assert batch.rows.shape == (2, TRADE_COLS)
    # [ts_us, price, size]
    assert batch.rows[0, 1] == 190.25
    assert batch.rows[0, 2] == 100.0
    assert batch.rows[1, 1] == 190.30
    assert abs(batch.rows[0, 0] - (MINUTE.timestamp() + 1.5) * 1_000_000.0) < 1.0


def test_quotes_round_trip() -> None:
    batch = decode_ticks(encode_ticks("AAPL", MINUTE, "quotes", QUOTES))
    assert batch.kind == "quotes"
    assert batch.rows.shape == (1, QUOTE_COLS)
    # [ts_us, bid, ask, bid_sz, ask_sz]
    assert batch.rows[0, 1] == 190.20
    assert batch.rows[0, 2] == 190.30
    assert batch.rows[0, 3] == 300.0
    assert batch.rows[0, 4] == 200.0


def test_empty_tick_batch_round_trips() -> None:
    batch = decode_ticks(encode_ticks("AAPL", MINUTE, "trades", []))
    assert batch.rows.shape == (0, TRADE_COLS)


def test_tick_decode_is_zero_copy_view() -> None:
    batch = decode_ticks(encode_ticks("AAPL", MINUTE, "trades", TRADES))
    assert batch.rows.base is not None  # a view over the frame buffer, not a parsed copy


def test_encode_ticks_validates_shape() -> None:
    with pytest.raises(ValueError, match="rows must be"):
        encode_tick_rows("AAPL", MINUTE, "trades", np.zeros((2, 5), dtype="<f8"))


def test_synthetic_minute_roundtrip() -> None:
    if not _redis_up():
        pytest.skip("redis not reachable")
    prefix = "mdtest_min"
    client = redis.Redis.from_url(URL)
    for stream in (BAR_STREAM, TRADES_STREAM):
        client.delete(md_stream_key(stream, "AAPL", prefix))
    publisher = MarketDataPublisher(url=URL, prefix=prefix)
    bars = [{"S": "AAPL", "o": 1.0, "h": 3.0, "l": 0.5, "c": 2.0, "v": 1000.0, "t": MINUTE.isoformat()}]
    published = publisher.publish_minute(bars, {"AAPL": TRADES}, {}, MINUTE)
    assert published == 2  # one bar frame + one trades frame

    consumer = MarketDataConsumer(["AAPL"], streams=[BAR_STREAM, TRADES_STREAM], url=URL, prefix=prefix, start="0")
    records = consumer.poll(block_ms=500, count=10)
    by_stream = {stream: record for stream, record in records}
    assert isinstance(by_stream[BAR_STREAM], BarRecord)
    assert by_stream[BAR_STREAM].close == 2.0
    assert isinstance(by_stream[TRADES_STREAM], TickBatch)
    assert by_stream[TRADES_STREAM].rows.shape == (2, TRADE_COLS)
    publisher.close()
    consumer.close()
    client.close()


def test_synthetic_tick_firehose_roundtrip() -> None:
    if not _redis_up():
        pytest.skip("redis not reachable")
    prefix = "mdtest_tick"
    client = redis.Redis.from_url(URL)
    client.delete(md_stream_key(TICK_TRADES_STREAM, "AAPL", prefix))
    publisher = MarketDataPublisher(url=URL, prefix=prefix)
    for trade in TRADES:
        publisher.publish_tick("AAPL", MINUTE, "trades", trade)

    consumer = MarketDataConsumer(["AAPL"], streams=[TICK_TRADES_STREAM], url=URL, prefix=prefix, start="0")
    records = consumer.poll(block_ms=500, count=10)
    assert len(records) == 2  # two individual trade frames
    for stream, record in records:
        assert stream == TICK_TRADES_STREAM
        assert isinstance(record, TickBatch)
        assert record.rows.shape == (1, TRADE_COLS)
    publisher.close()
    consumer.close()
    client.close()


def test_consumer_only_sees_subscribed_streams_and_symbols() -> None:
    if not _redis_up():
        pytest.skip("redis not reachable")
    prefix = "mdtest_scope"
    client = redis.Redis.from_url(URL)
    for symbol in ("AAPL", "MSFT"):
        for stream in (BAR_STREAM, TRADES_STREAM):
            client.delete(md_stream_key(stream, symbol, prefix))
    publisher = MarketDataPublisher(url=URL, prefix=prefix)
    bars = [
        {"S": "AAPL", "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1.0, "t": MINUTE.isoformat()},
        {"S": "MSFT", "o": 2.0, "h": 2.0, "l": 2.0, "c": 2.0, "v": 2.0, "t": MINUTE.isoformat()},
    ]
    publisher.publish_minute(bars, {"AAPL": TRADES}, {}, MINUTE)

    # Subscribe ONLY AAPL's bar stream — must not see MSFT, and must not see AAPL's trades.
    consumer = MarketDataConsumer(["AAPL"], streams=[BAR_STREAM], url=URL, prefix=prefix, start="0")
    records = consumer.poll(block_ms=500, count=10)
    assert len(records) == 1
    stream, record = records[0]
    assert stream == BAR_STREAM
    assert isinstance(record, BarRecord) and record.symbol == "AAPL"
    publisher.close()
    consumer.close()
    client.close()


def test_unknown_stream_rejected() -> None:
    with pytest.raises(ValueError, match="unknown md stream"):
        MarketDataConsumer(["AAPL"], streams=["not_a_stream"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
