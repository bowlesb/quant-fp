"""Consume feature vectors from per-symbol Redis streams — the strategy-container side.

A container constructs a BusConsumer with the symbols it cares about and calls ``poll()`` in a loop.
Each poll blocks briefly for new frames across exactly those streams and returns decoded
``FeatureVector`` objects (zero-copy payload). The consumer tracks each stream's last-seen id, so it
never re-reads a frame; by default it starts at "$" (only frames published after it connects). Decode
validates the schema fingerprint, so a container built against a different feature set fails loudly
rather than misreading offsets.
"""
from __future__ import annotations

from collections.abc import Iterable

import redis

from quantlib.bus.codec import decode
from quantlib.bus.publisher import DEFAULT_REDIS_URL, DEFAULT_STREAM_PREFIX, FRAME_FIELD, stream_key
from quantlib.bus.schema import BusSchema, default_schema
from quantlib.bus.vector import FeatureVector


class BusConsumer:
    """Reads + decodes feature vectors for a declared set of symbols."""

    def __init__(
        self,
        symbols: Iterable[str],
        url: str = DEFAULT_REDIS_URL,
        schema: BusSchema | None = None,
        prefix: str = DEFAULT_STREAM_PREFIX,
        start: str = "$",
    ) -> None:
        self._redis = redis.Redis.from_url(url)
        self._schema = schema or default_schema()
        self._prefix = prefix
        self._last_id: dict[str, str] = {stream_key(symbol, prefix): start for symbol in symbols}

    @property
    def schema(self) -> BusSchema:
        return self._schema

    def ping(self) -> bool:
        return bool(self._redis.ping())

    def symbols(self) -> list[str]:
        return [key.split(":", 1)[1] for key in self._last_id]

    def poll(self, block_ms: int = 1000, count: int = 100) -> list[FeatureVector]:
        """Block up to `block_ms` for new frames across the subscribed streams; return decoded vectors."""
        response = self._redis.xread(self._last_id, count=count, block=block_ms)
        vectors: list[FeatureVector] = []
        for raw_key, entries in response:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            for entry_id, fields in entries:
                entry_id_str = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                self._last_id[key] = entry_id_str
                vectors.append(decode(fields[FRAME_FIELD], self._schema))
        return vectors

    def close(self) -> None:
        self._redis.close()
