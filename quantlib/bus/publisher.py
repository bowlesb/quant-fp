"""Publish encoded feature vectors to Redis Streams — one stream per symbol (``fv:<symbol>``).

Per-symbol streams are the key to the "containers declare which tickers they ingest" requirement: a
consumer ``XREAD``s only the streams for its symbols, so it never pays to deserialize the other ~11k.
Each stream is trimmed to a bounded length (approximate MAXLEN) so total Redis memory is capped
regardless of uptime. Publishing is best-effort from the producer's side (see the capture hook): a bus
outage must never stall or crash the feature pipeline.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

import redis

from quantlib.bus.codec import encode
from quantlib.bus.registry import RedisSchemaBackend, SchemaRegistry, schema_key
from quantlib.bus.schema import BusSchema, default_schema

DEFAULT_REDIS_URL = os.environ.get("BUS_REDIS_URL", "redis://redis:6379/0")
DEFAULT_STREAM_PREFIX = "fv"
DEFAULT_MAXLEN = 240  # retain ~4h of minute frames per symbol stream (approximate trim, cheap)
FRAME_FIELD = b"d"


def stream_key(symbol: str, prefix: str = DEFAULT_STREAM_PREFIX) -> str:
    return f"{prefix}:{symbol}"


class BusPublisher:
    """Encodes + XADDs feature vectors to per-symbol Redis streams."""

    def __init__(
        self,
        url: str = DEFAULT_REDIS_URL,
        schema: BusSchema | None = None,
        maxlen: int = DEFAULT_MAXLEN,
        prefix: str = DEFAULT_STREAM_PREFIX,
    ) -> None:
        self._redis = redis.Redis.from_url(url)
        self._schema = schema or default_schema()
        self._maxlen = maxlen
        self._prefix = prefix
        self._registry = SchemaRegistry(RedisSchemaBackend(self._redis), compiled_schema=self._schema)
        self._schema_published = False

    @property
    def schema(self) -> BusSchema:
        return self._schema

    def ping(self) -> bool:
        return bool(self._redis.ping())

    def ensure_schema_published(self) -> None:
        """Publish-then-emit (B1): SET + CONFIRM ``bus:schema:<fp>`` BEFORE the first frame of this
        fingerprint, so no consumer can see a frame whose schema isn't yet resolvable. Guarded by a flag —
        run once per process (the publisher's schema is fixed for its life), zero steady-state cost."""
        if self._schema_published:
            return
        self._registry.publish(self._schema)
        if self._redis.get(schema_key(self._schema.fingerprint)) is None:  # confirm the write landed
            raise RuntimeError(
                f"failed to confirm bus:schema for fingerprint {self._schema.fingerprint:#018x} before emit"
            )
        self._schema_published = True

    def publish(self, symbol: str, minute: object, values: object) -> None:
        self.ensure_schema_published()
        frame = encode(symbol, minute, values, self._schema)  # type: ignore[arg-type]
        self._redis.xadd(
            stream_key(symbol, self._prefix), {FRAME_FIELD: frame}, maxlen=self._maxlen, approximate=True
        )

    def publish_many(self, items: Iterable[tuple[str, object, object]]) -> int:
        """Pipeline a batch of (symbol, minute, values) into one Redis round-trip. Returns the count."""
        self.ensure_schema_published()
        pipe = self._redis.pipeline(transaction=False)
        count = 0
        for symbol, minute, values in items:
            frame = encode(symbol, minute, values, self._schema)  # type: ignore[arg-type]
            pipe.xadd(
                stream_key(symbol, self._prefix), {FRAME_FIELD: frame}, maxlen=self._maxlen, approximate=True
            )
            count += 1
        pipe.execute()
        return count

    def close(self) -> None:
        self._redis.close()
