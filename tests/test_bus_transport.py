"""Integration test for the Redis-Streams transport: publish per-symbol frames and consume them back,
including ticker-scoped subscription (a consumer for one symbol never sees another's frames).

Skips cleanly when no Redis is reachable, so the unit suite still runs without infra. Point it at a
broker with BUS_REDIS_URL (defaults to the quant-redis service on the quant_default network).
"""
from __future__ import annotations

import datetime as dt
import os

import pytest
import redis

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import BusPublisher, stream_key
from quantlib.bus.schema import default_schema

URL = os.environ.get("BUS_REDIS_URL", "redis://quant-redis:6379/0")
PREFIX = "fvtest"
MINUTE = dt.datetime(2026, 6, 15, 14, 31, tzinfo=dt.timezone.utc)


def _redis_up() -> bool:
    try:
        redis.Redis.from_url(URL).ping()
        return True
    except (redis.exceptions.RedisError, OSError):
        return False


def _clear(symbols: list[str]) -> None:
    client = redis.Redis.from_url(URL)
    for symbol in symbols:
        client.delete(stream_key(symbol, PREFIX))
    client.close()


def test_publish_consume_roundtrip() -> None:
    if not _redis_up():
        pytest.skip("redis not reachable")
    _clear(["TST", "OTHR"])
    schema = default_schema()
    names = schema.names()
    publisher = BusPublisher(url=URL, schema=schema, prefix=PREFIX)
    publisher.publish("TST", MINUTE, {names[0]: 1.25, names[5]: -0.5})

    consumer = BusConsumer(["TST"], url=URL, schema=schema, prefix=PREFIX, start="0")
    vectors = consumer.poll(block_ms=500, count=10)
    match = [v for v in vectors if v.symbol == "TST"]
    assert match, "expected the published TST frame"
    assert match[0].value(names[0]) == 1.25
    assert match[0].value(names[5]) == -0.5
    assert match[0].minute == MINUTE
    publisher.close()
    consumer.close()


def test_consumer_only_sees_subscribed_symbols() -> None:
    if not _redis_up():
        pytest.skip("redis not reachable")
    _clear(["TST", "OTHR"])
    schema = default_schema()
    publisher = BusPublisher(url=URL, schema=schema, prefix=PREFIX)
    publisher.publish_many([("TST", MINUTE, {}), ("OTHR", MINUTE, {})])

    consumer = BusConsumer(["TST"], url=URL, schema=schema, prefix=PREFIX, start="0")
    vectors = consumer.poll(block_ms=500, count=10)
    symbols_seen = {v.symbol for v in vectors}
    assert symbols_seen == {"TST"}  # OTHR was published but not subscribed
    publisher.close()
    consumer.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
