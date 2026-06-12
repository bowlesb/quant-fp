-- GICS-style sector/industry map per symbol (task #20). Source: FMP /profile (the FMP key must be
-- wired into the env first — it currently lives only in the legacy project's encrypted secrets).
-- Separate from asset_metadata on purpose: different source (FMP vs Alpaca), different refresh
-- cadence (sector is slowly-changing — weekly is fine), and it keeps asset_metadata focused on
-- Alpaca tradability flags. Modeller JOINs on symbol at panel-build for sector-neutral momentum
-- (v1.3.0: mom_Xd minus the within-sector-within-timestamp mean) and future dispersion/beta features.
--
-- gics_sector is the 11-sector grain Modeller demeans within (REQUIRED for a mapped name). FMP
-- returns its own GICS-ALIGNED text taxonomy (e.g. "Technology", "Financial Services"), not literal
-- GICS codes — fine for categorical demeaning. Names FMP can't map (some ADRs / very recent listings)
-- get gics_sector = NULL; the consumer buckets NULL as "UNKNOWN" (never dropped). QA coverage gate:
-- null-sector rate over the live universe must stay < 5%.
CREATE TABLE IF NOT EXISTS asset_sector (
    symbol        text PRIMARY KEY,
    gics_sector   text,
    gics_industry text,
    source        text NOT NULL DEFAULT 'fmp',
    updated_at    timestamptz NOT NULL DEFAULT now()
);
