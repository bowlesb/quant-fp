-- Binary trust redesign (docs/TRUST_REDESIGN.md). Collapses the 4-state lifecycle
-- (PENDING/VALIDATED/DIVERGENT/NULL) into a binary TRUSTED/NON_TRUSTED grade keyed by (feature,version),
-- PERMANENT once earned, with the provenance to REPLAY the verdict (day, version, git commit, content
-- hash of the compute) and an append-only check history. Idempotent: re-runnable on the live DB.

-- feature_trust gains the binary gate + provenance. The legacy status/lifecycle_state/grade columns are
-- kept as diagnostics (no longer the gate) until nothing reads them.
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trust_state          text NOT NULL DEFAULT 'NON_TRUSTED';
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trust_reason         text;           -- deterministic | parity_1day | legacy_validated | random_check_failed
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trusted_at           timestamptz;
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trusted_day          date;
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trusted_git_commit   text;
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trusted_content_hash text;
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trust_value_rate     double precision;
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trust_tolerance      double precision;
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS trust_min_pass_rate  double precision;
ALTER TABLE feature_trust ADD COLUMN IF NOT EXISTS untrusted_at         timestamptz;    -- set when a random check un-trusts (audit; row stays, trust_state flips)

CREATE INDEX IF NOT EXISTS idx_feature_trust_state ON feature_trust (trust_state);

-- The legacy NOT-NULL grade columns become diagnostics under the binary model; give them defaults so a
-- binary-only INSERT (a deterministic feature that never had a legacy row) need not supply them.
ALTER TABLE feature_trust ALTER COLUMN status         SET DEFAULT 'unvalidated';
ALTER TABLE feature_trust ALTER COLUMN value_grade    SET DEFAULT 'U';
ALTER TABLE feature_trust ALTER COLUMN coverage_grade SET DEFAULT 'U';

-- Migrate the trust we already earned: VALIDATED -> TRUSTED. Everything else -> NON_TRUSTED (the default).
-- One-shot, guarded so a re-run never re-trusts a feature a random check later un-trusted.
UPDATE feature_trust
   SET trust_state = 'TRUSTED',
       trust_reason = COALESCE(trust_reason, 'legacy_validated'),
       trusted_at = COALESCE(trusted_at, now()),
       trusted_day = COALESCE(trusted_day, last_validated_day)
 WHERE lifecycle_state = 'VALIDATED'
   AND trust_state = 'NON_TRUSTED'
   AND untrusted_at IS NULL;

-- Append-only audit of every parity check behind trust: the initial grant AND every random re-check.
-- This is the reproducible record — "we trusted (feature,version) because on checked_day the stream
-- matched backfill at value_rate within tolerance, under code git_commit/content_hash". A random check
-- that later fails appends a row with passed=false, action='untrusted'.
CREATE TABLE IF NOT EXISTS feature_trust_check (
    id            bigserial PRIMARY KEY,
    feature       text NOT NULL,
    version       text NOT NULL,
    check_kind    text NOT NULL,                  -- 'initial' | 'random' | 'deterministic'
    checked_day   date,                           -- the clean day compared (NULL for deterministic)
    content_hash  text,                           -- compute-source hash at check time
    git_commit    text,                           -- repo commit at check time
    value_rate    double precision,               -- match rate over compared cells
    tolerance     double precision,               -- rtol applied
    min_pass_rate double precision,               -- threshold required to pass
    n_compared    bigint NOT NULL DEFAULT 0,
    passed        boolean NOT NULL,
    action        text NOT NULL,                  -- 'trusted' | 'reaffirmed' | 'untrusted' | 'noop'
    checked_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ftc_feature ON feature_trust_check (feature, version);
CREATE INDEX IF NOT EXISTS idx_ftc_day ON feature_trust_check (checked_day);
CREATE INDEX IF NOT EXISTS idx_ftc_kind ON feature_trust_check (check_kind);

-- Re-point the consumer surfaces at the binary gate. trusted_features == the SELECTION the backfill +
-- modelling agents gate on; now a single predicate (trust_state='TRUSTED').
CREATE OR REPLACE VIEW trusted_features AS
SELECT
    feature,
    version,
    trust_reason,
    trusted_day,
    trusted_git_commit,
    trusted_content_hash,
    trust_value_rate,
    trust_tolerance,
    trusted_at
FROM feature_trust
WHERE trust_state = 'TRUSTED';

CREATE OR REPLACE VIEW feature_trust_summary AS
SELECT
    trust_state,
    count(*)                                   AS n_features,
    count(*) FILTER (WHERE trust_reason = 'deterministic') AS n_deterministic,
    round(avg(trust_value_rate)::numeric, 5)   AS avg_trust_value_rate
FROM feature_trust
GROUP BY trust_state;

COMMENT ON VIEW trusted_features IS
    'Features that have EARNED trust (trust_state=TRUSTED): deterministic-by-construction, or stream==backfill '
    'within tolerance on >=1 clean RTH day. The SELECTION the backfill + modelling agents gate on. Binary, '
    'permanent per (feature,version); only a random-check failure un-trusts. See docs/TRUST_REDESIGN.md.';
