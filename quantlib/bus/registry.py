"""Resolve a frame's ``BusSchema`` by its fingerprint — the indirection that decouples a consumer from
the producer's exact feature set.

A frame carries its fingerprint; the producer publishes the matching schema to ``bus:schema:<fp>`` (the
``name -> offset`` map + per-field version). A consumer that sees a fingerprint it wasn't compiled against
fetches that schema once via a ``SchemaRegistry``, caches it per fingerprint (then every read is an O(1)
dict lookup), and resolves the features it needs by NAME against it. See docs/BUS_FEATURE_ACCESS.md §2.2.

``UnknownSchema`` is the *recoverable* signal that a fingerprint isn't resolvable YET — the publish may not
have propagated, or a reconnect is replaying retained frames. The consumer treats it as
retry-with-backoff, NEVER a hard stop (B1): a brief resolve lag must not kill a strategy container. Only an
unresolvable fingerprint after the bounded retries is an operational error to log.

The schema keys are tiny and few (one per fingerprint ever seen) and carry NO TTL — they must be exempt
from Redis eviction (run the bus Redis with ``maxmemory-policy noeviction``, or keep these keys on a
non-evictable backend). The frame streams are independently MAXLEN-trimmed, so capping their memory never
touches the schema keys (B4).
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

import redis

from quantlib.bus.schema import BusSchema, default_schema

logger = logging.getLogger(__name__)

SCHEMA_KEY_PREFIX = "bus:schema:"


def schema_key(fingerprint: int) -> str:
    return f"{SCHEMA_KEY_PREFIX}{fingerprint:#018x}"


class UnknownSchema(Exception):
    """A frame's fingerprint isn't resolvable yet (or at all) — recoverable; the consumer retries."""

    def __init__(self, fingerprint: int) -> None:
        self.fingerprint = fingerprint
        super().__init__(f"no schema published for fingerprint {fingerprint:#018x}")


class SchemaBackend(Protocol):
    """The minimal store a SchemaRegistry reads/writes — Redis in prod, a dict in tests."""

    def get(self, key: str) -> bytes | None: ...

    def set(self, key: str, value: str) -> None: ...


class DictSchemaBackend:
    """An in-process backend for tests (no Redis). Mutating it between resolve attempts models a
    publish that lands mid-poll — exactly the B1 retry path."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value.encode("utf-8")


class RedisSchemaBackend:
    """Redis-backed schema store. The schema keys carry NO TTL and MUST be eviction-exempt (B4): run the
    bus Redis with ``maxmemory-policy noeviction`` or place these keys on a non-evictable instance."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self._redis = redis_client

    def get(self, key: str) -> bytes | None:
        value = self._redis.get(key)
        return value if value is None else bytes(value)

    def set(self, key: str, value: str) -> None:
        self._redis.set(key, value)  # no TTL by design (B4)


class SchemaRegistry:
    """Fingerprint -> BusSchema, cached per fingerprint. Resolution falls back to the consumer's own
    compiled schema ONLY when the fingerprint matches it (the offsets are then knowably correct); for any
    other fingerprint there is no compiled fallback, only the backend lookup + retry."""

    def __init__(
        self,
        backend: SchemaBackend,
        *,
        compiled_schema: BusSchema | None = None,
        max_retries: int = 5,
        backoff_base_s: float = 0.05,
        backoff_cap_s: float = 1.0,
    ) -> None:
        self._backend = backend
        self._compiled = compiled_schema if compiled_schema is not None else default_schema()
        self._cache: dict[int, BusSchema] = {self._compiled.fingerprint: self._compiled}
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s

    def publish(self, schema: BusSchema) -> None:
        """Write a schema to the backend (the producer side; idempotent per fingerprint)."""
        self._backend.set(schema_key(schema.fingerprint), schema.to_json())

    def _fetch(self, fingerprint: int) -> BusSchema | None:
        raw = self._backend.get(schema_key(fingerprint))
        if raw is None:
            return None
        return BusSchema.from_json(raw.decode("utf-8"))

    def resolve(self, fingerprint: int) -> BusSchema:
        """Return the schema for ``fingerprint`` from cache or the backend. Raises ``UnknownSchema`` if it
        isn't in the backend (and isn't the compiled schema) — a SINGLE attempt, no retry. The retry
        policy lives in ``resolve_blocking`` so a caller can choose immediate-or-fail vs wait-for-publish."""
        cached = self._cache.get(fingerprint)
        if cached is not None:
            return cached
        fetched = self._fetch(fingerprint)
        if fetched is None:
            raise UnknownSchema(fingerprint)
        self._cache[fetched.fingerprint] = fetched
        return fetched

    def resolve_blocking(self, fingerprint: int) -> BusSchema:
        """Resolve with bounded retry-with-backoff (B1): a not-yet-propagated publish self-heals instead of
        propagating ``UnknownSchema``. Raises ``UnknownSchema`` only after the retries are exhausted, so the
        caller can log it as an operational error and keep polling OTHER frames — never a hard stop."""
        backoff_s = self._backoff_base_s
        for attempt in range(self._max_retries):
            try:
                return self.resolve(fingerprint)
            except UnknownSchema:
                if attempt == self._max_retries - 1:
                    raise
                logger.warning(
                    "schema %#018x not yet resolvable (attempt %d/%d) — retrying",
                    fingerprint,
                    attempt + 1,
                    self._max_retries,
                )
                time.sleep(min(backoff_s, self._backoff_cap_s))
                backoff_s *= 2
        raise UnknownSchema(fingerprint)  # unreachable; satisfies the type checker
