"""Source-data dependency for the ALT-DATA sources — ENSURE news + EDGAR are in the store before a backfill.

docs/SOURCE_DATA_DEPENDENCY.md. The sibling of ``source_dependency.ensure_inputs`` (which ensures the raw
MARKET layers bars/trades/quotes) extended to the two alt-data INPUT SOURCES a feature group can declare
(``FeatureGroup.required_sources`` → ``Source.NEWS`` / ``Source.EDGAR``):

  * NEWS  — the ``/store/news`` article tape (date-partitioned parquet + an append-only manifest;
            ``quantlib.data.news_store``). Consumed by ``news_sentiment`` via ``loaders.load_news_features``.
  * EDGAR — the Postgres ``filings`` event store (db/init/08_filings.sql; NO ``/store`` manifest).
            Consumed by ``edgar_filing_frequency`` via ``loaders.load_filings``.

The SAME contract as the market path: a feature-backfill job that computes an alt-data group over
``[start, end] × symbols`` calls ``ensure_sources`` FIRST to guarantee the declared source is current — under
the SAME per-source single-writer ``SourceIngestLock`` (a ``'news'`` lock + an ``'edgar'`` lock, distinct
rows in the one ingest-lock table) — then reads the source exclusively from the store. The three benefits
(A shared source, B tape stays current by construction, C parity strengthened) carry over verbatim.

WHY A SIBLING, NOT A FOLD INTO ``ensure_inputs``: the market path's hole unit is ``(symbol, date)`` against
the per-symbol-day raw manifest; the alt-data sources are NOT keyed that way:

  * NEWS is a multi-symbol-per-article DATE tape (one article carries a ``symbols`` list, stored once per
    UTC publish-date), so its hole unit is a DATE, detected via ``news_store.backfilled_dates`` (the SAME
    resume key a news backfill uses — one shared definition of "done", never a second drifting one).
  * EDGAR coverage is a DATE-range of the ``filings`` table (which ``available_at`` UTC dates have rows),
    detected by querying the table — there is no ``/store`` manifest to read.

So each alt source gets its own ADAPTER (hole detection) + its own FETCHER (the existing acquire engine),
wired exactly like ``default_fetcher`` does for the market layers, behind the same ``dry_run`` flag. The
hole HORIZON for an alt source is the backfill window EXPANDED by the consuming group's lookback (a news
feature on session day D reads articles back ``NEWS_LOOKBACK_DAYS``; an edgar feature reads filings back
``FILINGS_LOOKBACK_DAYS``) — declared by the group (``source_lookback_days``) so the backfill never
hardcodes it.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field

import psycopg

from quantlib.data.news_backfill import seed_day
from quantlib.data.news_fetchers import build_news_client
from quantlib.data.news_store import backfilled_dates
from quantlib.data.source_dependency import DEFAULT_LOCK_TIMEOUT_S, SourceIngestLock
from quantlib.features.base import Source
from quantlib.features.registry import REGISTRY
from quantlib.features.validation_db import DB_KWARGS

logger = logging.getLogger("source_inputs")

# A per-source fetcher acquires the missing DATES (over the resolved symbols) into the store and returns the
# count of dates fetched. News symbols matter (the Alpaca news query is symbol-scoped); EDGAR is symbol-scoped
# too (the submissions API is per-CIK). Production wires these to the real acquire engines; tests inject stubs.
SourceFetcher = Callable[[list[str], list[dt.date]], int]


def required_sources_for_groups(group_names: list[str]) -> frozenset[Source]:
    """Union of the INPUT SOURCES every named group DECLARES it needs (``required_sources``) — the full
    market + alt-data set. The backfill resolves the groups it is about to compute, then ensures this union:
    market sources via ``ensure_inputs``, alt-data sources via ``ensure_sources``. Raises ``KeyError`` (via
    the registry) on an unknown group — fail loud, never silently skip a group's source dependency."""
    sources: set[Source] = set()
    for name in group_names:
        sources |= REGISTRY.get_group(name).required_sources()
    return frozenset(sources)


def alt_sources_for_groups(group_names: list[str]) -> frozenset[Source]:
    """Just the NON-market (news/edgar) sources the named groups declare — what ``ensure_sources`` ensures.
    The market subset is the existing ``ensure_inputs`` path's job (``required_layers_for_groups``)."""
    return frozenset(
        source for source in required_sources_for_groups(group_names) if not source.is_market_layer
    )


def source_lookback_days_for_groups(group_names: list[str], source: Source) -> int:
    """The DEEPEST lookback (in calendar days) any named group reads ``source`` back over — the amount the
    backfill window must be EXPANDED so the alt-data tape is present for the consuming group's trailing
    windows. Derived from each group's ``source_lookback_days`` declaration (0 when a group does not consume
    the source), so the horizon lives WITH the group, not in a backfill-side constant."""
    return max(
        (REGISTRY.get_group(name).source_lookback_days(source) for name in group_names),
        default=0,
    )


def horizon_dates(start: dt.date, end: dt.date, lookback_days: int) -> list[dt.date]:
    """The inclusive UTC-date list ``[start - lookback_days, end]`` — the alt-data presence horizon. A
    news/edgar feature on session day D reads its source back ``lookback_days``, so a backfill over
    ``[start, end]`` must have the source present from ``start - lookback_days`` through ``end``."""
    first = start - dt.timedelta(days=lookback_days)
    return [first + dt.timedelta(days=offset) for offset in range((end - first).days + 1)]


@dataclass(frozen=True)
class SourceHoles:
    """The UTC dates of one alt-data ``source`` MISSING from the store over a backfill horizon — what must be
    fetched before the feature compute can read that source. ``dates`` is empty when the source is already
    fully present over the horizon (the share-the-source / already-current case)."""

    source: Source
    dates: list[dt.date]

    @property
    def is_empty(self) -> bool:
        return not self.dates


def find_news_holes(store: str, days: list[dt.date]) -> SourceHoles:
    """The UTC dates of ``days`` NOT yet seeded into ``/store/news``. REUSES the news backfill's resume key
    verbatim — ``news_store.backfilled_dates`` (the set of dates a BACKFILL manifest part has recorded, the
    no-news-date EMPTY-entry included) — so an "ensured" news date means EXACTLY what a news-backfill resume
    skips. Live-capture parts are intentionally NOT a completion signal (they append all session), so only a
    backfill-seeded date is "done"; a date the live feed touched but a backfill never swept is still a hole.
    """
    done = backfilled_dates(store)
    missing = [day for day in days if day.isoformat() not in done]
    return SourceHoles(source=Source.NEWS, dates=missing)


_EDGAR_COVERED_DATES_SQL = """
SELECT DISTINCT (available_at AT TIME ZONE 'UTC')::date AS covered_date
FROM filings
WHERE available_at >= %(start)s AND available_at < %(end_excl)s
"""


def edgar_covered_dates(days: list[dt.date]) -> set[dt.date]:
    """The UTC dates within ``days`` that ALREADY have at least one ``filings`` row — the EDGAR coverage
    adapter (the analogue of the manifest, since the filings table is the store). A single read-only query
    over the horizon's ``[min, max]`` range, bucketed to the UTC date of ``available_at`` (the point-in-time
    field). EDGAR has genuinely-empty days (weekends/holidays have no filings), so this is COVERAGE not
    completeness — see ``find_edgar_holes`` for why an empty day is treated as present, not a permanent hole.
    """
    if not days:
        return set()
    start = min(days)
    end_excl = max(days) + dt.timedelta(days=1)
    with psycopg.connect(**DB_KWARGS) as conn, conn.cursor() as cur:
        cur.execute(_EDGAR_COVERED_DATES_SQL, {"start": start.isoformat(), "end_excl": end_excl.isoformat()})
        rows = cur.fetchall()
    return {row[0] for row in rows}


def find_edgar_holes(
    days: list[dt.date],
    today: dt.date | None = None,
    settle_window_days: int = 1,
) -> SourceHoles:
    """The UTC dates of ``days`` the EDGAR ``filings`` table does not yet cover — the EDGAR hole adapter.

    Unlike the per-symbol-day market manifest, EDGAR coverage is read from the table itself
    (``edgar_covered_dates``). The subtlety is that EDGAR has genuinely-EMPTY days (no SEC filings disseminate
    on a weekend / federal holiday), so a blanket "no rows ⇒ hole" would churn those days forever. We resolve
    it the same way the market settle-window resolves a recent 0-row stub vs an aged no-data day:

      * a RECENT uncovered day (within ``settle_window_days`` of ``today``) IS a hole — the stream may simply
        not have polled it yet, or a backfill sweep is due, so it is re-checked until rows land or it ages out.
      * an AGED uncovered day is NOT a hole — EDGAR genuinely had no filings that calendar day (a weekend /
        holiday), so churning it would never terminate. The backfill's job is presence over the horizon, and
        a day SEC never disseminated on is "present" (correctly empty), not a permanent gap.

    ``today`` is injectable for deterministic tests; production passes the real UTC date. ``settle_window_days``
    defaults to 1 (EDGAR disseminates ~real-time, so a day older than a day with no rows is a true empty day),
    distinct from the market ``SETTLE_WINDOW_DAYS=5`` because the filings feed has no multi-day settle lag.
    """
    covered = edgar_covered_dates(days)
    as_of = today or dt.datetime.now(dt.timezone.utc).date()
    cutoff = as_of - dt.timedelta(days=settle_window_days)
    missing = [day for day in days if day not in covered and day >= cutoff]
    return SourceHoles(source=Source.EDGAR, dates=missing)


def find_source_holes(
    raw_store: str,
    source: Source,
    days: list[dt.date],
    today: dt.date | None = None,
) -> SourceHoles:
    """Dispatch hole detection to the right alt-data adapter: NEWS → ``find_news_holes`` (store manifest),
    EDGAR → ``find_edgar_holes`` (filings-table coverage). Raises for a market source — those go through
    ``source_dependency.find_holes`` (per-symbol-day), not here."""
    if source is Source.NEWS:
        return find_news_holes(raw_store, days)
    if source is Source.EDGAR:
        return find_edgar_holes(days, today=today)
    raise ValueError(
        f"{source.value} is a market layer — use source_dependency.find_holes, not find_source_holes"
    )


@dataclass
class EnsureSourcesReport:
    """What ``ensure_sources`` did, per alt-data source — the proof a feature backfill can read it from the
    store. ``holes_before`` / ``fetched_dates`` let the caller assert the source is now current (or log how
    much was patched). When every source's ``holes_before`` is 0 the backfill shared an already-current
    source (benefit A) and nothing was downloaded."""

    sources: tuple[Source, ...]
    holes_before: dict[Source, int] = field(default_factory=dict)
    fetched_dates: dict[Source, int] = field(default_factory=dict)
    skipped_locked: tuple[Source, ...] = ()

    @property
    def all_present(self) -> bool:
        """True iff no source had any hole left unfetched (nothing skipped due to a held lock). A caller that
        REQUIRES the source before computing asserts this."""
        return not self.skipped_locked


def news_fetcher(store: str) -> SourceFetcher:
    """The PRODUCTION news fetcher: seed the missing UTC dates for ``symbols`` into ``/store/news`` via the
    existing news acquire engine (``news_backfill.seed_day``), the SAME path a news backfill uses (de-dup by
    article id → idempotent). Closes over ``store`` + builds one Alpaca news client for the run. Returns the
    count of dates seeded."""
    client = build_news_client(os.environ["ALPACA_KEY_ID"], os.environ["ALPACA_SECRET_KEY"])

    def _fetch(symbols: list[str], days: list[dt.date]) -> int:
        for day in days:
            seed_day(client, symbols, day, store)
        return len(days)

    return _fetch


def ensure_sources(
    raw_store: str,
    sources: frozenset[Source],
    symbols: list[str],
    days: list[dt.date],
    agent_id: str,
    fetchers: dict[Source, SourceFetcher],
    today: dt.date | None = None,
    lock_timeout_s: int = DEFAULT_LOCK_TIMEOUT_S,
    dry_run: bool = True,
) -> EnsureSourcesReport:
    """ENSURE every alt-data ``source`` is current in the store over ``days`` (× ``symbols`` where the engine
    is symbol-scoped) — patch only the missing DATES. The alt-data analogue of ``ensure_inputs``.

    For each source, in a stable order: acquire the per-source single-writer lock, detect missing dates via
    the source's adapter, fetch ONLY those dates via the source's ``fetcher`` (de-dup → idempotent; a second
    job over the same horizon fetches nothing), release the lock. A source whose lock is held by ANOTHER live
    job is SKIPPED (recorded in ``skipped_locked``) rather than blocking — the caller decides whether to wait
    and retry; serialization is preserved either way (no two writers on one source at once).

    ``dry_run`` (default) takes no DB lock and calls no fetcher — it only reports holes, so a job can see what
    WOULD be fetched. Production passes ``dry_run=False`` with the real ``fetchers``. EDGAR has no in-process
    fetcher (the submissions backfill is the ``services/edgar`` operator job, run separately under the same
    ``'edgar'`` lock) — a source with no fetcher entry is HOLE-DETECTED + reported, and in a live run logs the
    dates the operator job must cover, but is not fetched in-process (recorded in ``skipped_locked`` so a
    require-all caller sees the source is not yet self-served)."""
    ordered = sorted(sources, key=lambda source: source.value)
    report = EnsureSourcesReport(sources=tuple(ordered))
    skipped: list[Source] = []
    lock = SourceIngestLock(agent_id=agent_id, timeout_s=lock_timeout_s, dry_run=dry_run)
    for source in ordered:
        holes = find_source_holes(raw_store, source, days, today=today)
        report.holes_before[source] = len(holes.dates)
        if holes.is_empty:
            logger.info(
                "ensure_sources: source=%s already current over horizon (shared source)", source.value
            )
            report.fetched_dates[source] = 0
            continue
        fetcher = fetchers.get(source)
        if fetcher is None:
            logger.warning(
                "ensure_sources: source=%s has %d holes but NO in-process fetcher — the %s operator job must "
                "cover these dates (run it under the '%s' ingest lock); reporting, not fetching",
                source.value,
                len(holes.dates),
                source.value,
                source.value,
            )
            report.fetched_dates[source] = 0
            skipped.append(source)
            continue
        if not lock.claim(source):
            logger.warning(
                "ensure_sources: source=%s ingest lock held by another job — skipping (caller may retry)",
                source.value,
            )
            skipped.append(source)
            continue
        try:
            if dry_run:
                logger.info(
                    "ensure_sources: DRY-RUN source=%s would fetch %d dates (%s..%s) — no fetch",
                    source.value,
                    len(holes.dates),
                    holes.dates[0].isoformat(),
                    holes.dates[-1].isoformat(),
                )
                report.fetched_dates[source] = 0
                continue
            logger.info(
                "ensure_sources: source=%s fetching %d dates (%s..%s)",
                source.value,
                len(holes.dates),
                holes.dates[0].isoformat(),
                holes.dates[-1].isoformat(),
            )
            report.fetched_dates[source] = fetcher(symbols, holes.dates)
        finally:
            lock.release(source)
    report.skipped_locked = tuple(skipped)
    return report


def ensure_sources_for_groups(
    raw_store: str,
    group_names: list[str],
    symbols: list[str],
    start: dt.date,
    end: dt.date,
    agent_id: str,
    dry_run: bool = True,
) -> EnsureSourcesReport:
    """The CLI-facing one-call form: resolve the ALT-DATA sources ``group_names`` DECLARE, EXPAND each
    source's horizon by the deepest lookback any group reads it over, then ``ensure_sources`` them into
    ``raw_store`` over that horizon using the production fetchers.

    The alt-data step-1 a feature-backfill CLI runs BEFORE computing a news/edgar group (the contract that
    makes the abstraction deliver A/B/C for the alt sources too). Only NEWS has an in-process production
    fetcher (``news_fetcher``); EDGAR is hole-detected + reported (its submissions backfill is the
    ``services/edgar`` operator job). ``dry_run`` (default) reports holes without fetching or a DB lock."""
    alt = alt_sources_for_groups(group_names)
    if not alt:
        return EnsureSourcesReport(sources=())
    fetchers: dict[Source, SourceFetcher] = {}
    if Source.NEWS in alt and not dry_run:
        fetchers[Source.NEWS] = news_fetcher(raw_store)
    # Each source can have a different lookback; ensure_sources takes ONE day list, so we ensure the SUPERSET
    # horizon (the deepest lookback across the alt sources) — a date present for the deeper source is a no-op
    # for the shallower one (idempotent), and the per-source adapter only flags that source's own holes.
    lookback = max(source_lookback_days_for_groups(group_names, source) for source in alt)
    days = horizon_dates(start, end, lookback)
    return ensure_sources(
        raw_store,
        alt,
        symbols,
        days,
        agent_id=agent_id,
        fetchers=fetchers,
        dry_run=dry_run,
    )
