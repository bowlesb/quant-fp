"""Consume feature vectors from per-symbol Redis streams — the strategy-container side.

A container constructs a BusConsumer with the symbols it cares about and calls ``poll()`` in a loop.
Each poll blocks briefly for new frames across exactly those streams and returns decoded
``FeatureVector`` objects (zero-copy payload). The consumer tracks each stream's last-seen id, so it
never re-reads a frame; by default it starts at "$" (only frames published after it connects). Decode
validates the schema fingerprint, so a container built against a different feature set fails loudly
rather than misreading offsets.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

import redis

from quantlib.bus.codec import decode, decode_view
from quantlib.bus.compat import FeatureReq, contract_key, contract_to_json
from quantlib.bus.publisher import DEFAULT_REDIS_URL, DEFAULT_STREAM_PREFIX, FRAME_FIELD, stream_key
from quantlib.bus.registry import RedisSchemaBackend, SchemaRegistry
from quantlib.bus.schema import BusSchema, default_schema
from quantlib.bus.vector import FeatureVector
from quantlib.bus.view import FeatureView

_T = TypeVar("_T")


class BusConsumer:
    """Reads feature frames for a declared set of symbols.

    ``poll_views`` is the decoupled path: each frame is decoded into a ``FeatureView`` whose schema is
    resolved BY FINGERPRINT via the registry (so an additive/restructured producer feature set is read by
    NAME without a rebuild). ``poll`` is the legacy exact-fingerprint path, kept as a seam during migration.
    """

    def __init__(
        self,
        symbols: Iterable[str],
        url: str = DEFAULT_REDIS_URL,
        schema: BusSchema | None = None,
        prefix: str = DEFAULT_STREAM_PREFIX,
        start: str = "$",
        registry: SchemaRegistry | None = None,
    ) -> None:
        self._redis = redis.Redis.from_url(url)
        self._schema = schema or default_schema()
        self._prefix = prefix
        self._last_id: dict[str, str] = {stream_key(symbol, prefix): start for symbol in symbols}
        self._registry = registry or SchemaRegistry(
            RedisSchemaBackend(self._redis), compiled_schema=self._schema
        )

    @property
    def schema(self) -> BusSchema:
        return self._schema

    @property
    def registry(self) -> SchemaRegistry:
        return self._registry

    def ping(self) -> bool:
        return bool(self._redis.ping())

    def publish_contract(self, strategy: str, contract: Sequence[FeatureReq]) -> None:
        """Publish a strategy's declared (name, version) feature contract to ``strategy:features:<name>``
        so the pre-deploy compat gate reads the LIVE set (B3)."""
        self._redis.set(contract_key(strategy), contract_to_json(contract))

    def symbols(self) -> list[str]:
        return [key.split(":", 1)[1] for key in self._last_id]

    def poll(self, block_ms: int = 1000, count: int = 100) -> list[FeatureVector]:
        """Legacy exact-fingerprint path: block up to `block_ms`, return decoded `FeatureVector`s."""
        return self._poll(block_ms, count, lambda frame: decode(frame, self._schema))

    def poll_views(self, block_ms: int = 1000, count: int = 100) -> list[FeatureView]:
        """The decoupled path: block up to `block_ms`, return `FeatureView`s resolved by fingerprint."""
        return self._poll(block_ms, count, lambda frame: decode_view(frame, self._registry))

    def _poll(
        self,
        block_ms: int,
        count: int,
        decode_frame: Callable[[bytes], _T],
    ) -> list[_T]:
        response = self._redis.xread(self._last_id, count=count, block=block_ms)
        results: list[_T] = []
        for raw_key, entries in response:
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            for entry_id, fields in entries:
                entry_id_str = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                self._last_id[key] = entry_id_str
                results.append(decode_frame(fields[FRAME_FIELD]))
        return results

    def close(self) -> None:
        self._redis.close()
