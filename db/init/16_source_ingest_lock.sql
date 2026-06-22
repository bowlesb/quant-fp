-- Source-data dependency abstraction: the single-writer raw-INGEST lock (docs/SOURCE_DATA_DEPENDENCY.md).
-- DESIGN-ONLY until the Lead activates the live wiring; additive (CREATE ... IF NOT EXISTS), re-runnable on
-- the live DB, NO data/behavior impact until ensure_inputs runs with dry_run=False.

-- INGEST LOCK — one writer per raw LAYER (bars/trades/quotes). ensure_inputs claims the layer's lock before
-- detecting+fetching holes, so two concurrent feature backfills never double-fetch the same symbol-days or
-- race the append-only raw manifest (the shared resource). Scoped per LAYER (not per symbol-day) because the
-- acquire engines fan out symbol-days internally; the manifest append is what must be serialized. Mirrors the
-- within_day_assignment claim/heartbeat/release/reclaim pattern: PK on layer = one active owner per layer; a
-- stale heartbeat reclaims a dead job's lock (status=timed_out) so a layer is never stuck forever.
CREATE TABLE IF NOT EXISTS source_ingest_lock (
    layer         text        NOT NULL,             -- 'bars' | 'trades' | 'quotes' (RawLayer.value)
    agent_id      text        NOT NULL,             -- the backfill job that owns the layer's ingest
    claimed_at    timestamptz NOT NULL DEFAULT now(),
    heartbeat_at  timestamptz NOT NULL DEFAULT now(),  -- the job bumps this during a long fetch; a stale lock is reclaimable
    status        text        NOT NULL DEFAULT 'active',  -- 'active' | 'released' | 'timed_out'
    released_at   timestamptz,
    PRIMARY KEY (layer)                             -- ONE active writer per layer, enforced by the DB
);
CREATE INDEX IF NOT EXISTS idx_sil_agent  ON source_ingest_lock (agent_id);
CREATE INDEX IF NOT EXISTS idx_sil_status ON source_ingest_lock (status);

COMMENT ON TABLE source_ingest_lock IS
    'Single-writer raw-ingest lock (docs/SOURCE_DATA_DEPENDENCY.md). PK on layer = one writer per raw layer '
    '(bars/trades/quotes); ensure_inputs holds it while patching that layer''s manifest holes so two feature '
    'backfills never race the append-only raw manifest. heartbeat_at drives the dead-job reclaim (status=timed_out).';
