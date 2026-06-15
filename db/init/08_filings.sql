-- SEC EDGAR filings store (Phase 1 of the EDGAR alt-data direction — COLLECTION ONLY, no features).
-- Design + rationale: docs/EDGAR_INGESTION.md. A standalone ingestor (services/edgar) polls the SEC
-- current-filings Atom feed every ~5s, dedupes by accession, maps CIK->ticker, and UPSERTs here.
--
-- THE PARITY CRUX — THREE DISTINCT TIMESTAMPS (the fix the prior project missed):
-- The old schema stored a single `filed_at` and CONFLATED it with the Atom <updated> dissemination
-- time. For a look-ahead-safe event-clock feature ("minutes since last 8-K") we MUST separate:
--   * filed_at     — the company's filing DATE (often a bare date, midnight UTC). METADATA ONLY.
--                    Keying a feature off this LEAKS look-ahead: a 10-K stamped 2026-06-14 was not
--                    publicly knowable at 2026-06-14 00:00 — it disseminated later that day.
--   * accepted_at  — SEC acceptance datetime (from the submissions API; ~real but is SEC-internal
--                    acceptance, not necessarily the public-feed moment). METADATA.
--   * available_at — WHEN THE FILING BECAME PUBLICLY VISIBLE. For LIVE collection this is the Atom
--                    <updated> instant — the first moment a real-time consumer could have known. THIS
--                    is the point-in-time field every future feature keys off. NOT NULL by contract.
-- Backfill must replay the EXACT available_at recorded live; for deep history collected before we ran,
-- the best reconstructable dissemination time (submissions acceptanceDateTime) is used and FLAGGED
-- lower-confidence via available_at_source — never silently passed off as the live feed time.
--
-- HYPERTABLE KEY: TimescaleDB requires the partitioning column to be part of any PRIMARY KEY/UNIQUE
-- constraint, so the PK is (accession_number, available_at) rather than accession_number alone. This
-- is NOT a weakening of the dedup contract: available_at for a given accession is fixed at first sight
-- (we never rewrite it — see the ingestor's ON CONFLICT DO NOTHING / coalesce-preserve logic), so the
-- compound key dedupes by accession in practice. Amendments (10-K/A, 8-K/A) carry their OWN accession
-- and OWN available_at — each is its own event, never backfilled onto the original.
--
-- symbol is NULLABLE on purpose: an unmapped CIK (recent listing / ADR / the daily company_tickers.json
-- not yet refreshed) is KEPT with symbol=NULL, never dropped — the mapping may resolve later and a
-- backfill re-UPSERT can fill it in.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS filings (
    accession_number     text        NOT NULL,           -- SEC accession (e.g. 0001234567-26-000001)
    cik                  text        NOT NULL,            -- zero-padded 10-digit SEC CIK
    symbol               text,                            -- mapped via cik_mapper; NULL if unmapped (KEPT)
    form_type            text        NOT NULL,            -- '8-K','10-K','4','SC 13D',...
    company_name         text,                            -- filer name (from Atom <title> / submissions)
    filed_at             timestamptz,                     -- company filing date (METADATA — do NOT key features off this)
    accepted_at          timestamptz,                     -- SEC acceptance datetime (METADATA)
    available_at         timestamptz NOT NULL,            -- WHEN WE SAW IT PUBLIC — the point-in-time, look-ahead-safe field
    available_at_source  text        NOT NULL DEFAULT 'atom_feed',  -- 'atom_feed' (live, high-conf) | 'submissions_accepted' (backfill, lower-conf)
    link                 text,                            -- canonical filing-index URL
    source               text        NOT NULL DEFAULT 'stream',     -- 'stream' (live feed) | 'backfill' (submissions API)
    discovered_at        timestamptz NOT NULL DEFAULT now(),        -- our wall-clock receipt (ops/debug; NOT point-in-time)
    PRIMARY KEY (accession_number, available_at)
);

SELECT create_hypertable('filings', 'available_at', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);

-- Feature/lookup access is "all filings for symbol X up to time T" (the event clock), so index on
-- (symbol, available_at). available_at leads the natural time-range scans the modeller will run.
CREATE INDEX IF NOT EXISTS filings_symbol_available_idx ON filings (symbol, available_at DESC);
-- Backfill/CIK-remap path resolves an unmapped or changed CIK; index it for those sweeps.
CREATE INDEX IF NOT EXISTS filings_cik_available_idx ON filings (cik, available_at DESC);
-- Form-type filtering (the event-clock is per (symbol, form_type)).
CREATE INDEX IF NOT EXISTS filings_form_available_idx ON filings (form_type, available_at DESC);
