-- Feature validation ledger — the PERSISTENT RECORD that each real-time-collected feature was
-- backfill-verified, accumulated into a per-feature trust registration (docs/VALIDATION_LEDGER.md).
--
-- WHY here and not parquet: the feature VALUES live in the parquet store (10k symbols x ~390 minutes x
-- ~519 features/day — far too large for the DB). These are the small, relational, QUERYABLE
-- verification RECORDS — per-(feature,day) rollups, the per-feature trust grade, and the rare diverging
-- cells. They are what a dashboard, a training-export gate, and an operator actually query ("is
-- volume_zscore_30m trustworthy yet?", "which features diverged on Monday?"), so a relational home with
-- indexes beats parquet. The `validate.py` job computes the stream-vs-backfill comparison over the
-- parquet store (PINNED to the day's universe membership, RTH-scoped) and UPSERTs the result here.
--
-- GRAIN CHOICE: a literal per-cell flag is ~2e9 rows/day, so we do NOT store one row per verified cell.
-- A cell is "match" by construction unless it appears in feature_validation_exception (mismatches and
-- extra-live cells are RARE by design); the per-(feature,day,tier) rollup carries the counts. So every
-- collected datapoint's verified status is recoverable: exception row -> diverged; else covered by the
-- rollup -> verified-match (missing cells are the rollup's n_missing_live, i.e. warmup/coverage gaps).
--
-- HONEST SCOPE: a `certified` status means the live compute path reproduces backfill on the RECENT
-- overlap — NOT that deep-history backfill equals what live would have collected (point-in-time
-- reference / splits / vendor tape revisions drift). We prioritize ticker breadth over temporal depth
-- and are explicit about that limit.

-- Per (feature, version, day, tier): one feature's stream-vs-backfill verification for one settled day.
-- "This real-time feature was backfill-verified on this day; value_rate of compared cells agreed."
-- tier = the day's liquidity tier (1 best, what we trade); tier 0 is reserved for an all-tiers rollup.
CREATE TABLE IF NOT EXISTS feature_validation_day (
    feature        text   NOT NULL,
    version        text   NOT NULL,
    day            date   NOT NULL,
    tier           int    NOT NULL,
    method         text   NOT NULL,                 -- 'tolerance' | 'distributional'
    n_compared     bigint NOT NULL DEFAULT 0,       -- cells both sides non-null (the value-rate denom)
    n_match        bigint NOT NULL DEFAULT 0,       -- within the feature's tolerance (EXACT for flags)
    n_mismatch     bigint NOT NULL DEFAULT 0,       -- value disagreement (-> exceptions)
    n_extra_live   bigint NOT NULL DEFAULT 0,       -- live emitted a value, backfill did not (-> exceptions)
    n_missing_live bigint NOT NULL DEFAULT 0,       -- backfill had it, live did not (capture gap incl. warmup)
    value_rate     double precision,                -- n_match / n_compared (NULL if n_compared = 0)
    coverage_rate  double precision,                -- n_compared / (n_compared + n_missing_live)
    worst_abs_err  double precision,
    validated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature, version, day, tier)
);
CREATE INDEX IF NOT EXISTS idx_fvd_day ON feature_validation_day (day);
CREATE INDEX IF NOT EXISTS idx_fvd_feature ON feature_validation_day (feature, version);

-- Per (feature, version): the durable TRUST REGISTRATION — a pure recompute over
-- feature_validation_day (idempotent, self-healing: re-validate a day -> its rows change -> recompute).
-- THE record a training export intersects with so a model never trains on a feature production can't
-- reproduce. This table is retention-exempt (tiny: one row per feature/version).
CREATE TABLE IF NOT EXISTS feature_trust (
    feature                text   NOT NULL,
    version                text   NOT NULL,
    status                 text   NOT NULL,         -- unvalidated | validating | certified | divergent
    value_grade            text   NOT NULL,         -- A>=0.9999  B>=0.999  C>=0.99  F below  U unvalidated
    coverage_grade         text   NOT NULL,
    method                 text,
    n_days_validated       int    NOT NULL DEFAULT 0,
    lifetime_compared      bigint NOT NULL DEFAULT 0,
    lifetime_match         bigint NOT NULL DEFAULT 0,
    lifetime_value_rate    double precision,
    lifetime_coverage_rate double precision,
    last_validated_day     date,
    last_day_value_rate    double precision,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature, version)
);

-- The rare diverging cells (mismatch | extra_live) — the audit trail behind a low value_rate, so we
-- can see exactly WHICH symbol/minute/value broke a feature-day (e.g. a split-corrupted multi-day
-- feature shows up as a wall of mismatches on one symbol). Matches and missing cells are counts in the
-- rollup, NOT rows, so this stays small.
CREATE TABLE IF NOT EXISTS feature_validation_exception (
    feature        text   NOT NULL,
    symbol         text   NOT NULL,
    ts             timestamptz NOT NULL,            -- the cell's minute, UTC
    day            date   NOT NULL,
    tier           int,
    status         text   NOT NULL,                 -- 'mismatch' | 'extra_live'
    stream_value   double precision,                -- what we collected live
    backfill_value double precision,                -- what backfill produced
    abs_err        double precision,
    rel_err        double precision,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature, symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_fve_day ON feature_validation_exception (day);
CREATE INDEX IF NOT EXISTS idx_fve_feature ON feature_validation_exception (feature);
