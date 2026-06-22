-- Source-data dependency abstraction: the single-writer source-INGEST lock (docs/SOURCE_DATA_DEPENDENCY.md).
-- DESIGN-ONLY until the Lead activates the live wiring; additive (CREATE ... IF NOT EXISTS), re-runnable on
-- the live DB, NO data/behavior impact until ensure_inputs / ensure_sources runs with dry_run=False.

-- INGEST LOCK — one writer per INPUT SOURCE. ensure_inputs (market) / ensure_sources (any source) claims the
-- source's lock before detecting+fetching holes, so two concurrent feature backfills never double-fetch the
-- same units or race the shared append target (the raw manifest, the /store/news manifest, or the EDGAR
-- filings table). Scoped per SOURCE (not per symbol-day) because the acquire engines fan out internally; the
-- shared append is what must be serialized. The 'layer' column holds the source key verbatim: the three
-- market layers ('bars'/'trades'/'quotes', RawLayer.value) AND the alt-data sources ('news'/'edgar',
-- Source.value) all live in this one table (their keys never collide). Mirrors the within_day_assignment
-- claim/heartbeat/release/reclaim pattern: PK on layer = one active owner per source; a stale heartbeat
-- reclaims a dead job's lock (status=timed_out) so a source is never stuck forever. (The 'layer' column name
-- is kept for back-compat with the already-applied table — it is the generic source key, not market-only.)
CREATE TABLE IF NOT EXISTS source_ingest_lock (
    layer         text        NOT NULL,             -- 'bars'|'trades'|'quotes' (RawLayer.value) | 'news'|'edgar' (Source.value)
    agent_id      text        NOT NULL,             -- the backfill job that owns the source's ingest
    claimed_at    timestamptz NOT NULL DEFAULT now(),
    heartbeat_at  timestamptz NOT NULL DEFAULT now(),  -- the job bumps this during a long fetch; a stale lock is reclaimable
    status        text        NOT NULL DEFAULT 'active',  -- 'active' | 'released' | 'timed_out'
    released_at   timestamptz,
    PRIMARY KEY (layer)                             -- ONE active writer per source, enforced by the DB
);
CREATE INDEX IF NOT EXISTS idx_sil_agent  ON source_ingest_lock (agent_id);
CREATE INDEX IF NOT EXISTS idx_sil_status ON source_ingest_lock (status);

COMMENT ON TABLE source_ingest_lock IS
    'Single-writer source-ingest lock (docs/SOURCE_DATA_DEPENDENCY.md). PK on layer = one writer per input '
    'source — the market layers (bars/trades/quotes) AND the alt-data sources (news/edgar). ensure_inputs / '
    'ensure_sources holds it while patching that source''s holes so two feature backfills never race the '
    'shared append (raw manifest / news manifest / filings table). heartbeat_at drives the dead-job reclaim.';
