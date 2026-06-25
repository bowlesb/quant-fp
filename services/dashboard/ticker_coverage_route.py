"""Per-ticker coverage as a dashboard read route — the human-visible wrapper over ops/ticker_coverage.

``ops/ticker_coverage.py`` (#463) answers, for one symbol: which feature groups cover it, on which sources
(stream / backfill / both), how far back, and — optionally — each covered feature's trust/lifecycle state
(incl. DIVERGENT). It is a CLI; this module adapts it to a short-TTL-cached JSON snapshot the dashboard serves.

SLIM-SAFE (the #462 lesson): this module imports ONLY ``ticker_coverage``, which imports engine-free — it
pulls NO ``quantlib.features`` at module load. The trust join is OPTIONAL (``with_trust``) and uses
ticker_coverage's LAZY, call-time ``validation_db`` import, so the feature engine is touched only when a trust
read is actually requested, never at dashboard boot. The default (store-only) path is a pure /store read with
zero engine and zero DB dependency. This module deliberately does NOT import ``feature_grid`` / ``store_grid``
(those pull the engine at module top and would re-create the boot cliff).

The store is mounted read-only at ``$STORE_ROOT`` (``/store``) in the dashboard, exactly as the coverage grid
worker reads it. A single symbol's report is a bounded set of partition reads (the bounded file-sample reused
from ticker_coverage), cheap enough for the request path; a small per-(symbol, with_trust) TTL cache keeps a
re-request off the store entirely.
"""

from __future__ import annotations

import time

from ticker_coverage import (
    DEFAULT_STORE_ROOT,
    PartitionStoreReader,
    build_report,
    read_trust_by_feature,
)

# Per-(symbol, with_trust) snapshot cache. A ticker's coverage moves only as the store gains a day or a
# backfill lands (minutes-to-hours cadence), so a short TTL keeps a repeat request off the store while staying
# fresh to the operator. Small and bounded by the number of distinct symbols actually queried.
_CACHE_TTL_SECONDS = 60.0
_cache: dict[tuple[str, bool], tuple[float, dict[str, object]]] = {}


def _store_reader() -> PartitionStoreReader:
    return PartitionStoreReader(DEFAULT_STORE_ROOT)


def ticker_coverage_snapshot(symbol: str, with_trust: bool = False) -> dict[str, object]:
    """The per-ticker coverage report for ``symbol`` (upper-cased), short-TTL cached per (symbol, with_trust).

    ``with_trust`` joins the feature_trust table (lazy engine/DB import inside ticker_coverage) for per-feature
    TRUSTED / DIVERGENT state; omit it for a pure store-only read. Raises whatever the store/DB read raises
    (the route maps those to 503 ``booting`` like the other read routes)."""
    normalized = symbol.upper()
    key = (normalized, with_trust)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    trust_by_feature = read_trust_by_feature() if with_trust else None
    report = build_report(_store_reader(), normalized, trust_by_feature)
    _cache[key] = (now, report)
    return report
