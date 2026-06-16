"""Test the capture publish hook's assembly: per-GROUP frames -> one packed per-SYMBOL vector on the bus.

Uses a hand-built toy schema + real Redis (skips if unreachable), so it exercises the BusHook background
thread end-to-end without needing the full registry. Verifies cross-group assembly and that a symbol
missing from a group gets NaN for that group's features (not a silent zero).
"""
from __future__ import annotations

import datetime as dt
import os
import time

import polars as pl
import pytest
import redis

from quantlib.bus.consumer import BusConsumer
from quantlib.bus.publisher import BusPublisher, stream_key
from quantlib.bus.schema import BusField, BusSchema
from quantlib.features.bus_hook import BusHook

URL = os.environ.get("BUS_REDIS_URL", "redis://quant-redis:6379/0")
PREFIX = "fvhook"
MINUTE = dt.datetime(2026, 6, 15, 14, 31, tzinfo=dt.timezone.utc)


def _toy_schema() -> BusSchema:
    return BusSchema(
        [
            BusField(group="momentum", name="momentum_fast_1", offset=0, version="1.0.0"),
            BusField(group="momentum", name="momentum_slow_1", offset=1, version="1.0.0"),
            BusField(group="volatility", name="realized_vol_5m", offset=2, version="2.1.0"),
        ]
    )


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


def _poll_until(consumer: BusConsumer, want: int, timeout_s: float = 3.0) -> list:
    deadline = time.time() + timeout_s
    collected: list = []
    while time.time() < deadline and len(collected) < want:
        collected.extend(consumer.poll(block_ms=200, count=50))
    return collected


def test_hook_assembles_groups_into_one_vector() -> None:
    if not _redis_up():
        pytest.skip("redis not reachable")
    schema = _toy_schema()
    _clear(["AAPL", "MSFT"])
    publisher = BusPublisher(url=URL, schema=schema, prefix=PREFIX)
    hook = BusHook(publisher=publisher)

    momentum = pl.DataFrame(
        {"symbol": ["AAPL", "MSFT"], "minute": [MINUTE, MINUTE],
         "momentum_fast_1": [1.0, 9.0], "momentum_slow_1": [2.0, 8.0]}
    )
    volatility = pl.DataFrame(
        {"symbol": ["AAPL"], "minute": [MINUTE], "realized_vol_5m": [0.5]}  # MSFT absent here
    )
    hook.submit(MINUTE, [("momentum", momentum), ("volatility", volatility)])

    consumer = BusConsumer(["AAPL", "MSFT"], url=URL, schema=schema, prefix=PREFIX, start="0")
    vectors = _poll_until(consumer, want=2)
    by_symbol = {vec.symbol: vec for vec in vectors}
    assert set(by_symbol) == {"AAPL", "MSFT"}
    # AAPL: present in both groups -> all three features set
    assert by_symbol["AAPL"].momentum.momentum_fast_1 == 1.0
    assert by_symbol["AAPL"].momentum.momentum_slow_1 == 2.0
    assert by_symbol["AAPL"].volatility.realized_vol_5m == 0.5
    # MSFT: absent from volatility -> realized_vol_5m is NaN (not a silent 0)
    assert by_symbol["MSFT"].momentum.momentum_fast_1 == 9.0
    assert by_symbol["MSFT"].array[2] != by_symbol["MSFT"].array[2]  # NaN
    publisher.close()
    consumer.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
