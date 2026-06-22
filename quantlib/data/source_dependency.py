"""Source-data dependency abstraction — ENSURE raw inputs are in the store before a feature backfill.

docs/SOURCE_DATA_DEPENDENCY.md. The contract (Ben's task #74): a feature-backfill job that wants feature
group X over ``[start, end] × symbols`` must NOT re-download Alpaca source repeatedly. It FIRST calls
``ensure_inputs`` to guarantee the raw layers X declares (``FeatureGroup.required_raw_layers`` →
bars/trades/quotes) are present in ``/store/raw`` over that horizon — patching only the HOLES (manifest
dedup) — then reads source EXCLUSIVELY from the store (``quantlib.features.raw_loaders``).

This cleanly SEPARATES two stages that were tangled before:
  1. acquiring raw INPUTS into the store (this module + the existing acquire engines), and
  2. computing the FEATURE from the stored source (``materialize`` / ``selective_backfill``).

Why a reusable abstraction (not per-job re-download):
  * (A) other feature backfills SHARE the source — quotes fetched for ``quote_spread`` are already there
    for ``liquidity`` tomorrow; the manifest dedup (``resumable_done_keys``) means a second job fetches
    nothing.
  * (B) it FORCES the raw tape to always be up to date — any hole over a backfill's horizon is patched
    before the feature compute runs, so the deep tape only ever fills in.
  * (C) it STRENGTHENS parity certification — the backfill compute path reads the SAME stored source the
    realtime path's aggregates derive from, so a live-vs-backfill mismatch can never be a "different
    download" artifact; the source bytes are one shared substrate.

Hole detection REUSES the existing acquire-side dedup verbatim (``load_manifest`` + ``resumable_done_keys``
+ the ``SETTLE_WINDOW_DAYS`` / ``FORCE_REFETCH_SYMBOLS`` / ``MIN_SETTLED_TICK_ROWS`` policy), so an
"ensured" key means EXACTLY what a resume means — no second, drifting definition of "done".

A SINGLE-WRITER LOCK (``SourceIngestLock``, DB-backed, mirroring ``within_day_assignment``) serializes raw
ingest PER (layer): only one job fetches a given layer's tape at a time, so two concurrent feature
backfills never double-fetch the same symbol-days or race the append-only manifest. The lock is scoped per
LAYER (not per symbol-day) because the acquire engines already fan out symbol-days internally and the
manifest append is the shared resource to serialize.
"""
from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg

from quantlib.data.fast_backfill import (DEFAULT_PROCESSES,
                                         DEFAULT_THREADS_PER_PROCESS,
                                         run_tier_fast)
from quantlib.data.raw_backfill import (BARS_CHUNK_DAYS,
                                        BARS_SYMBOLS_PER_REQUEST,
                                        DEFAULT_MAX_WORKERS,
                                        FORCE_REFETCH_SYMBOLS,
                                        MIN_SETTLED_TICK_ROWS,
                                        QUOTES_CHUNK_DAYS, SETTLE_WINDOW_DAYS,
                                        TRADES_CHUNK_DAYS, BackfillConfig,
                                        fetch_bars_tier)
from quantlib.data.raw_store import load_manifest, resumable_done_keys
from quantlib.features.base import RawLayer, Source
from quantlib.features.registry import REGISTRY
from quantlib.features.validation_db import DB_KWARGS

logger = logging.getLogger("source_dependency")

DEFAULT_LOCK_TIMEOUT_S = 1800  # a layer-ingest lock whose heartbeat is older than this is reclaimable

# A fetcher acquires (layer, symbols, days) into the store and returns (partitions_written, bytes_written).
# Production wires this to the real acquire engines (``default_fetcher``); tests inject a recording stub.
Fetcher = Callable[[RawLayer, list[str], list[dt.date]], tuple[int, int]]


def _config_for(store: str, symbols: list[str], days: list[dt.date]) -> BackfillConfig:
    """A ``BackfillConfig`` pinned to an EXPLICIT (symbols, days) hole set, budget-unbounded for the patch.

    ``ensure_inputs`` already narrowed the work to the manifest holes, so the config carries no universe /
    months / ranking — just the chunking + concurrency the acquire engines need. ``budget_bytes`` is huge
    because the live_monitor mem/disk guard (the ``quant-backfill``-named container) is the real safety
    valve for a patch run, not a per-call byte budget."""
    return BackfillConfig(
        store=store,
        months=0,
        top_trades=0,
        top_quotes=0,
        budget_bytes=1 << 62,
        symbols=symbols,
        days=None,
        start=days[0] if days else None,
        end=days[-1] if days else None,
        max_workers=DEFAULT_MAX_WORKERS,
        bars_symbols_per_request=BARS_SYMBOLS_PER_REQUEST,
        bars_chunk_days=BARS_CHUNK_DAYS,
        trades_chunk_days=TRADES_CHUNK_DAYS,
        quotes_chunk_days=QUOTES_CHUNK_DAYS,
        processes=DEFAULT_PROCESSES,
        threads_per_process=DEFAULT_THREADS_PER_PROCESS,
    )


def default_fetcher(store: str) -> Fetcher:
    """The PRODUCTION fetcher: dispatch a layer's holes to the existing acquire engines, store-bound.

    bars → ``fetch_bars_tier`` (multi-symbol + date-range requests); trades/quotes → ``run_tier_fast``
    (the multiprocess direct-httpx tick engine). Both write the SAME partition layout + append-only
    manifest ``ensure_inputs`` reads, so the dedup is end-to-end consistent. Closes over ``store`` so the
    ``Fetcher`` signature stays ``(layer, symbols, days)``."""

    def _fetch(layer: RawLayer, symbols: list[str], days: list[dt.date]) -> tuple[int, int]:
        if layer is RawLayer.BARS:
            return fetch_bars_tier(_config_for(store, symbols, days), symbols, days)
        return run_tier_fast(store, layer.value, symbols, days)

    return _fetch


def required_layers_for_groups(group_names: list[str]) -> frozenset[RawLayer]:
    """Union of the raw layers every named group DECLARES it needs (``required_raw_layers``).

    The backfill driver resolves the groups it is about to compute, then ensures THIS union of layers —
    one ``ensure_inputs`` pass covers a whole multi-group backfill (bars fetched once even if five groups
    need them). Raises ``KeyError`` (via the registry) on an unknown group — fail loud, never silently
    skip a group's source dependency."""
    layers: set[RawLayer] = set()
    for name in group_names:
        layers |= REGISTRY.get_group(name).required_raw_layers()
    return frozenset(layers)


@dataclass(frozen=True)
class InputHoles:
    """The (symbol, date_iso) units MISSING for one raw layer over a backfill horizon — what must be
    fetched before the feature compute can read that layer from the store. ``units`` is empty when the
    layer is already fully present (the share-the-source / already-up-to-date case)."""

    layer: RawLayer
    units: list[tuple[str, str]]

    @property
    def is_empty(self) -> bool:
        return not self.units


def find_holes(
    store: str,
    layer: RawLayer,
    symbols: list[str],
    days: list[dt.date],
    today: dt.date | None = None,
) -> InputHoles:
    """The (symbol, date) units of ``layer`` NOT yet safely present in ``store`` over ``symbols × days``.

    REUSES the acquire-side resume logic verbatim: ``load_manifest`` + ``resumable_done_keys`` with the
    SAME ``SETTLE_WINDOW_DAYS`` / ``FORCE_REFETCH_SYMBOLS`` / ``MIN_SETTLED_TICK_ROWS`` policy a raw resume
    uses, so an "ensured" key means exactly what a "done" resume key means — a recent 0-row / stub entry
    inside the settle window is a HOLE (re-fetched until the real tape lands), while a genuinely-thin aged
    day is NOT a hole (never churned). One shared definition of done, not a second drifting one.

    ``today`` is injectable for deterministic tests; production passes the real UTC date.
    """
    manifest = load_manifest(store, layer.value)
    as_of = today or dt.datetime.now(dt.timezone.utc).date()
    done = resumable_done_keys(
        manifest,
        as_of,
        SETTLE_WINDOW_DAYS,
        force_refetch_symbols=FORCE_REFETCH_SYMBOLS,
        min_settled_rows=MIN_SETTLED_TICK_ROWS,
    )
    day_isos = [day.isoformat() for day in days]
    units = [
        (symbol, day_iso)
        for symbol in symbols
        for day_iso in day_isos
        if (symbol, day_iso) not in done
    ]
    return InputHoles(layer=layer, units=units)


@dataclass
class EnsureReport:
    """What ``ensure_inputs`` did, per layer — the proof a feature backfill can read source from the store.

    ``holes_before`` / ``fetched_units`` let the caller assert the tape is now complete (or log how much
    was patched). When every layer's ``holes_before`` is 0 the backfill shared an already-complete tape
    (benefit A) and nothing was downloaded."""

    layers: tuple[RawLayer, ...]
    holes_before: dict[RawLayer, int] = field(default_factory=dict)
    fetched_units: dict[RawLayer, int] = field(default_factory=dict)
    partitions_written: dict[RawLayer, int] = field(default_factory=dict)
    bytes_written: dict[RawLayer, int] = field(default_factory=dict)
    skipped_locked: tuple[RawLayer, ...] = ()

    @property
    def all_present(self) -> bool:
        """True iff no layer had any hole left unfetched (nothing skipped due to a held lock and every
        attempted layer was fetched). A caller that REQUIRES the source before computing asserts this."""
        return not self.skipped_locked


def ensure_inputs(
    store: str,
    layers: frozenset[RawLayer],
    symbols: list[str],
    days: list[dt.date],
    agent_id: str,
    fetcher: Fetcher,
    today: dt.date | None = None,
    lock_timeout_s: int = DEFAULT_LOCK_TIMEOUT_S,
    dry_run: bool = True,
) -> EnsureReport:
    """ENSURE every raw ``layer`` is present in ``store`` over ``symbols × days`` — patch only the HOLES.

    The contract a feature-backfill job calls FIRST (then reads source exclusively from the store). For
    each layer, in a stable order: acquire the per-layer single-writer lock, detect holes against the
    manifest, fetch ONLY the holes via ``fetcher`` (manifest dedup → idempotent; a second job over the
    same horizon fetches nothing), release the lock. A layer whose lock is held by ANOTHER live job is
    SKIPPED (recorded in ``skipped_locked``) rather than blocking — the caller decides whether to wait and
    retry; serialization is preserved either way (no two writers on one layer's manifest at once).

    ``dry_run`` (default) takes no DB lock and calls no fetcher — it only reports holes, so a job can see
    what WOULD be fetched. Production passes ``dry_run=False`` with the real ``default_fetcher``.

    Idempotent + memory-bounded: re-running after a full fetch finds zero holes and is a no-op; the heavy
    work lives in ``fetcher`` (the acquire engines, already process-pooled + budget-guarded), not here.
    """
    ordered = sorted(layers, key=lambda layer: layer.value)
    report = EnsureReport(layers=tuple(ordered))
    skipped: list[RawLayer] = []
    lock = SourceIngestLock(agent_id=agent_id, timeout_s=lock_timeout_s, dry_run=dry_run)
    for layer in ordered:
        holes = find_holes(store, layer, symbols, days, today=today)
        report.holes_before[layer] = len(holes.units)
        if holes.is_empty:
            logger.info("ensure_inputs: layer=%s already complete over horizon (shared source)", layer.value)
            report.fetched_units[layer] = 0
            continue
        if not lock.claim(layer):
            logger.warning(
                "ensure_inputs: layer=%s ingest lock held by another job — skipping (caller may retry)",
                layer.value,
            )
            skipped.append(layer)
            continue
        try:
            symbols_needed = sorted({symbol for symbol, _ in holes.units})
            days_needed = sorted({dt.date.fromisoformat(day_iso) for _, day_iso in holes.units})
            if dry_run:
                logger.info(
                    "ensure_inputs: DRY-RUN layer=%s would fetch %d holes (%d symbols x %d days) — no fetch",
                    layer.value,
                    len(holes.units),
                    len(symbols_needed),
                    len(days_needed),
                )
                report.fetched_units[layer] = 0
                continue
            logger.info(
                "ensure_inputs: layer=%s fetching %d holes (%d symbols x %d days)",
                layer.value,
                len(holes.units),
                len(symbols_needed),
                len(days_needed),
            )
            partitions, bytes_written = fetcher(layer, symbols_needed, days_needed)
            report.fetched_units[layer] = len(holes.units)
            report.partitions_written[layer] = partitions
            report.bytes_written[layer] = bytes_written
        finally:
            lock.release(layer)
    report.skipped_locked = tuple(skipped)
    return report


def ensure_inputs_for_groups(
    raw_store: str,
    group_names: list[str],
    symbols: list[str],
    days: list[dt.date],
    agent_id: str,
    dry_run: bool = True,
) -> EnsureReport:
    """The CLI-facing one-call form: resolve the raw layers ``group_names`` DECLARE, then ``ensure_inputs``
    them into ``raw_store`` over ``symbols × days`` using the production ``default_fetcher``.

    This is the step-1 a feature-backfill CLI runs BEFORE computing (the contract that makes the abstraction
    actually deliver A/B/C): the union of declared layers is patched (only holes) so the feature compute
    reads source exclusively from the store. ``dry_run`` (default) reports holes without fetching or a DB
    lock — the live activation (real fetch + real lock) is the gated step the caller opts into."""
    layers = required_layers_for_groups(group_names)
    return ensure_inputs(
        raw_store,
        layers,
        symbols,
        days,
        agent_id=agent_id,
        fetcher=default_fetcher(raw_store),
        dry_run=dry_run,
    )


_CLAIM = """
INSERT INTO source_ingest_lock (layer, agent_id, claimed_at, heartbeat_at, status)
VALUES (%(layer)s, %(agent_id)s, now(), now(), 'active')
ON CONFLICT (layer) DO UPDATE SET
  agent_id=EXCLUDED.agent_id, claimed_at=now(), heartbeat_at=now(), status='active', released_at=NULL
WHERE source_ingest_lock.status <> 'active'
   OR source_ingest_lock.heartbeat_at < now() - (%(timeout_s)s || ' seconds')::interval
RETURNING agent_id
"""

_HEARTBEAT = """
UPDATE source_ingest_lock SET heartbeat_at = now()
WHERE layer = %(layer)s AND agent_id = %(agent_id)s AND status = 'active'
RETURNING layer
"""

_RELEASE = """
UPDATE source_ingest_lock SET status='released', released_at=now()
WHERE layer = %(layer)s AND agent_id = %(agent_id)s AND status='active'
RETURNING layer
"""

_RECLAIM_STALE = """
UPDATE source_ingest_lock SET status='timed_out'
WHERE status='active' AND heartbeat_at < now() - (%(timeout_s)s || ' seconds')::interval
RETURNING layer
"""


def _execute(sql: str, params: dict[str, object]) -> list[tuple]:
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.commit()
    return rows


@dataclass
class SourceIngestLock:
    """The single-writer raw-ingest lock, scoped PER LAYER (bars/trades/quotes). DB-backed, mirroring the
    ``within_day_assignment`` claim/heartbeat/release/reclaim pattern (PK ``layer`` = one writer per layer,
    a stale heartbeat reclaims a dead job's lock so a layer is never stuck forever).

    PER LAYER, not per symbol-day, because the acquire engines fan out symbol-days internally and the
    APPEND-ONLY MANIFEST is the shared resource two writers would race; serializing the layer serializes
    that. ``dry_run`` (default) logs intent + returns success without a DB write — the live activation is
    the Lead's gated step, exactly as ``within_day_assignment``.

    The PK column is plain ``text``, so the SAME lock table serializes the alt-data SOURCES too: a
    ``Source.NEWS`` / ``Source.EDGAR`` lock is just another row keyed on its ``.value`` (``'news'`` /
    ``'edgar'``), distinct from the ``bars``/``trades``/``quotes`` rows. Every method therefore accepts a
    ``RawLayer`` OR a ``Source`` (both expose ``.value``) — one single-writer mechanism across all sources.
    """

    agent_id: str
    timeout_s: int = DEFAULT_LOCK_TIMEOUT_S
    dry_run: bool = True

    def claim(self, layer: RawLayer | Source) -> bool:
        """Claim ``layer`` for this agent. True if claimed (free / released / a timed-out lock), False if
        another agent holds a LIVE lock. dry_run logs + returns True with no DB write."""
        params: dict[str, object] = {
            "layer": layer.value,
            "agent_id": self.agent_id,
            "timeout_s": self.timeout_s,
        }
        if self.dry_run:
            logger.info("DRY-RUN claim layer=%s agent=%s (no DB write)", layer.value, self.agent_id)
            return True
        return bool(_execute(_CLAIM, params))

    def heartbeat(self, layer: RawLayer | Source) -> bool:
        """Bump the lock's heartbeat (liveness) during a long fetch. True if the agent still holds it."""
        params: dict[str, object] = {"layer": layer.value, "agent_id": self.agent_id}
        if self.dry_run:
            logger.info("DRY-RUN heartbeat layer=%s agent=%s (no DB write)", layer.value, self.agent_id)
            return True
        return bool(_execute(_HEARTBEAT, params))

    def release(self, layer: RawLayer | Source) -> bool:
        """Release the lock (on fetch done). True if it was this agent's active lock."""
        params: dict[str, object] = {"layer": layer.value, "agent_id": self.agent_id}
        if self.dry_run:
            logger.info("DRY-RUN release layer=%s agent=%s (no DB write)", layer.value, self.agent_id)
            return True
        return bool(_execute(_RELEASE, params))

    def reclaim_stale(self) -> list[str]:
        """Time out every active lock whose heartbeat is older than ``timeout_s`` (dead-job reclaim).
        Returns the reclaimed layer names. dry_run is a no-op."""
        if self.dry_run:
            logger.info("DRY-RUN reclaim_stale timeout=%ds (no DB write)", self.timeout_s)
            return []
        return [row[0] for row in _execute(_RECLAIM_STALE, {"timeout_s": self.timeout_s})]
