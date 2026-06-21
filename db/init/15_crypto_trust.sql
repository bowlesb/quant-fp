-- Crypto-namespaced trust + within-day cert ledgers (docs/CRYPTO_E2E.md §1).
--
-- The off-hours crypto rehearsal exercises the SAME trust/within-day machinery as equity, but on SEPARATED
-- data. The equity trust model (12_trust_binary.sql / 13_within_day_parity.sql) is keyed (feature, version)
-- GLOBALLY with no asset dimension, so a crypto grant written there would COLLIDE with the equity grant for
-- the same feature name (volume_zscore_1m equity != volume_zscore_1m crypto: different universe/calendar/tape
-- density, genuinely different parity). We therefore give crypto its OWN ledger, carrying an explicit
-- asset_class column, so cross-asset contamination is impossible by construction (equity never reads these
-- tables). Additive + idempotent (CREATE ... IF NOT EXISTS): re-runnable on the live DB, zero equity impact.
--
-- This MIRRORS the equity schema shape (12_trust_binary.sql feature_trust + feature_trust_check;
-- 13_within_day_parity.sql within_day_parity_cert) so the crypto sweep reuses the equity grading logic
-- unchanged — only the target table differs. If crypto graduates to a first-class asset class, the
-- consolidation is to fold these into one asset_class-keyed table (docs/CRYPTO_E2E.md §1, option A).

-- The crypto binary trust gate. Same columns as feature_trust's trust surface, PLUS asset_class in the PK so
-- a crypto grant is a row DISTINCT from any equity grant for the same (feature, version).
CREATE TABLE IF NOT EXISTS crypto_feature_trust (
    asset_class          text NOT NULL DEFAULT 'crypto',  -- the separation key; 'crypto' for this ledger
    feature              text NOT NULL,
    version              text NOT NULL,
    trust_state          text NOT NULL DEFAULT 'NON_TRUSTED',  -- TRUSTED | NON_TRUSTED
    trust_reason         text,           -- parity_1day | within_day_parity | deterministic
    trusted_at           timestamptz,
    trusted_day          date,
    trusted_git_commit   text,
    trusted_content_hash text,
    trust_value_rate     double precision,
    trust_tolerance      double precision,
    trust_min_pass_rate  double precision,
    untrusted_at         timestamptz,    -- set if a re-check un-trusts (row stays, state flips)
    updated_at           timestamptz NOT NULL DEFAULT now(),
    -- One row per (asset_class, feature, version): crypto trust never overwrites equity trust.
    PRIMARY KEY (asset_class, feature, version)
);
CREATE INDEX IF NOT EXISTS idx_crypto_trust_state ON crypto_feature_trust (asset_class, trust_state);

COMMENT ON TABLE crypto_feature_trust IS
    'Crypto-namespaced binary trust ledger (docs/CRYPTO_E2E.md). Mirrors feature_trust''s trust surface but '
    'keyed (asset_class, feature, version) so a crypto grant is physically separate from the equity grant for '
    'the same feature name — the separation principle (SHARED machinery, SEPARATED data by asset class). '
    'trust_reason=parity_1day means: on trusted_day the crypto live (source=stream) emit matched its batch '
    'recompute (source=backfill) within tolerance on the captured crypto tape (emit==recompute parity; NOT '
    'independent-source agreement — crypto has no second tape, see docs/CRYPTO_E2E.md §3).';

-- Append-only audit of every crypto parity check behind a grant (mirrors feature_trust_check).
CREATE TABLE IF NOT EXISTS crypto_trust_check (
    id            bigserial PRIMARY KEY,
    asset_class   text NOT NULL DEFAULT 'crypto',
    feature       text NOT NULL,
    version       text NOT NULL,
    check_kind    text NOT NULL,                  -- 'initial' | 'random' | 'within_day'
    checked_day   date,
    content_hash  text,
    git_commit    text,
    value_rate    double precision,
    tolerance     double precision,
    min_pass_rate double precision,
    n_compared    bigint NOT NULL DEFAULT 0,
    passed        boolean NOT NULL,
    action        text NOT NULL,                  -- 'trusted' | 'reaffirmed' | 'untrusted' | 'noop'
    checked_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_crypto_ftc_feature ON crypto_trust_check (asset_class, feature, version);
CREATE INDEX IF NOT EXISTS idx_crypto_ftc_day     ON crypto_trust_check (checked_day);

-- Crypto within-day parity certification stamps (mirrors within_day_parity_cert; docs/CRYPTO_E2E.md §4
-- Phase-1 step 4). One row per (asset_class, feature, version, cert_day). FEEDS crypto_feature_trust the
-- same way the equity cert feeds feature_trust (trust_reason=within_day_parity).
CREATE TABLE IF NOT EXISTS crypto_within_day_parity_cert (
    asset_class     text        NOT NULL DEFAULT 'crypto',
    feature         text        NOT NULL,
    version         text        NOT NULL,
    group_name      text        NOT NULL,
    cert_day        date        NOT NULL,
    status          text        NOT NULL,     -- 'certified' | 'fix_pending' | 'defected'
                                              --            | 'skipped_unsettled' | 'skipped_contaminated'
    stable_cycles   int         NOT NULL DEFAULT 0,
    window_minutes  int         NOT NULL DEFAULT 0,
    value_rate      double precision,
    n_clean_symbols int,
    n_compared      bigint      NOT NULL DEFAULT 0,
    tolerance       double precision,
    min_pass_rate   double precision,
    settle_lag_min  double precision,
    git_commit      text,
    content_hash    text,
    reason          text,
    certified_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_class, feature, version, cert_day)
);
CREATE INDEX IF NOT EXISTS idx_crypto_wdpc_day    ON crypto_within_day_parity_cert (cert_day);
CREATE INDEX IF NOT EXISTS idx_crypto_wdpc_status ON crypto_within_day_parity_cert (asset_class, status);

COMMENT ON TABLE crypto_within_day_parity_cert IS
    'Crypto within-day parity certification (docs/CRYPTO_E2E.md). Mirrors within_day_parity_cert, keyed by '
    'asset_class so crypto certs never collide with equity. A certified row is the evidence the crypto binary '
    'trust grant consumes (trust_reason=within_day_parity).';

-- Crypto trust summary view (mirrors feature_trust_summary's shape, scoped to the crypto ledger).
DROP VIEW IF EXISTS crypto_feature_trust_summary;
CREATE VIEW crypto_feature_trust_summary AS
SELECT
    asset_class,
    trust_state,
    count(*)                                 AS n_features,
    round(avg(trust_value_rate)::numeric, 5) AS avg_trust_value_rate
FROM crypto_feature_trust
GROUP BY asset_class, trust_state;
