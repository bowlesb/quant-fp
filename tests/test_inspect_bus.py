"""inspect_bus tests: the NaN-aware stats are network-free; the synthetic round-trip uses the bus and
skips cleanly when Redis is unreachable. The synthetic path proves a published vector is read back
COMPLETE with a matching fingerprint — the verification the CLI exists to perform."""
from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pytest
import redis

from quantlib.bus.schema import default_schema
from quantlib.bus.vector import FeatureVector
from strategies.tools.inspect_bus import parse_symbols, run_synthetic, vector_stats

URL = os.environ.get("BUS_REDIS_URL", "redis://quant-redis:6379/0")
SCHEMA = default_schema()
MINUTE = dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)


def _redis_up() -> bool:
    try:
        redis.Redis.from_url(URL).ping()
        return True
    except (redis.exceptions.RedisError, OSError):
        return False


def test_parse_symbols() -> None:
    assert parse_symbols("aapl, msft ,NVDA") == ["AAPL", "MSFT", "NVDA"]
    assert parse_symbols("") == []


def test_vector_stats_counts_nans() -> None:
    array = np.full(SCHEMA.n_features, np.nan, dtype="<f8")
    array[0] = 1.0
    array[1] = 3.0
    vector = FeatureVector(SCHEMA, "AAPL", MINUTE, array, SCHEMA.fingerprint)
    stats = vector_stats(vector)
    assert stats["nan_count"] == SCHEMA.n_features - 2
    assert stats["finite_count"] == 2
    assert stats["min"] == 1.0
    assert stats["max"] == 3.0


def test_synthetic_roundtrip_reads_complete_vectors() -> None:
    if not _redis_up():
        pytest.skip("redis not reachable")
    verified = run_synthetic(["AAPL", "MSFT"], URL, full=False)
    assert verified == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
