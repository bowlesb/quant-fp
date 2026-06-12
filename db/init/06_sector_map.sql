-- Sector/industry map per symbol (task #20). Source: FMP /profile (the FMP key must be wired into
-- the env first — it currently lives only in the legacy project's encrypted secrets).
--
-- Separate from asset_metadata on purpose: different source (FMP vs Alpaca), different refresh
-- cadence (sector is slowly-changing — weekly is fine), and it keeps asset_metadata focused on
-- Alpaca tradability flags. Modeller JOINs on symbol at FEATURE-COMPUTE time (a lookup, NOT a
-- precomputed column on the 5.5M-row feature_vectors — that would duplicate slowly-changing
-- metadata and couple a sector refresh to a panel rebuild). Powers v1.3.0 sector-neutral momentum
-- (mom_Xd minus the within-sector-within-timestamp mean) and future industry-level neutralization.
--
-- TAXONOMY (documented per the Manager's directive so a future "why not strict GICS codes?" has an
-- answer on file): `sector` holds FMP's GICS-ALIGNED TEXT LABELS (e.g. "Technology",
-- "Financial Services"), NOT literal GICS sector codes. Categorical grouping is all the demeaning
-- math needs, so the label taxonomy is sufficient and we do NOT pay for strict GICS codes. `sector`
-- is the 11-bucket grain demeaned within; `industry` is the finer grain for a follow-up experiment.
-- Names FMP can't map (some ADRs / very recent listings) get sector = NULL; the consumer buckets
-- NULL as "UNKNOWN" and never drops them. QA coverage gate: null-sector rate over the live universe
-- must stay < 5%. The fetcher should also snapshot the DISTINCT sector-label set so QA can alarm if
-- FMP silently renames a sector (which would split a demeaning group).
CREATE TABLE IF NOT EXISTS sector_map (
    symbol      text PRIMARY KEY,
    sector      text,
    industry    text,
    source      text NOT NULL DEFAULT 'fmp',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
