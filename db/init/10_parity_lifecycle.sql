-- Parity-validation LIFECYCLE — the contamination-aware trust state machine + the parity-defect backlog
-- (docs/PARITY_LIFECYCLE.md). Built ADDITIVELY on top of 07_feature_validation.sql: the nightly sweep
-- (quantlib.features.validation_sweep) writes the cell-for-cell comparison into feature_validation_day /
-- feature_validation_exception exactly as before, then this layer derives a CLEAN-DAY-ONLY trust grade
-- and files a defect for any feature that fails parity on CLEAN symbol-days.
--
-- WHY a separate state machine from feature_trust.status. The original status (validating/certified/
-- divergent) grades over EVERY compared cell, so one capture-contaminated day (a mid-session restart
-- leaving minute gaps) can flip a CORRECT windowed feature to "divergent". The lifecycle below grades
-- over CLEAN (feature, symbol, day) comparisons only, so a feature is DIVERGENT only when it fails the
-- real compute_latest()==compute() parity on days where the live capture was complete. The columns are
-- added to feature_trust additively (existing rows keep working; the legacy status is untouched).

-- Additive lifecycle columns on the existing per-(feature,version) trust registration. These coexist with
-- the legacy status/value_grade/coverage_grade columns; the lifecycle_state is the contamination-aware
-- grade downstream consumers gate on (trusted_feature_names()).
ALTER TABLE feature_trust
    ADD COLUMN IF NOT EXISTS lifecycle_state    text,    -- PENDING | VALIDATED | DIVERGENT | RETIRED
    ADD COLUMN IF NOT EXISTS clean_days         int  NOT NULL DEFAULT 0,  -- # clean symbol-days compared
    ADD COLUMN IF NOT EXISTS clean_days_passed  int  NOT NULL DEFAULT 0,  -- # clean days where parity held
    ADD COLUMN IF NOT EXISTS clean_value_rate   double precision,         -- match rate over CLEAN comparisons only
    ADD COLUMN IF NOT EXISTS lifecycle_updated_at timestamptz NOT NULL DEFAULT now();

-- The PARITY-DEFECT BACKLOG — the "investigate" half of the QUARANTINE policy. When a feature becomes
-- DIVERGENT (fails parity on clean days) it is NOT deleted: it keeps being computed/collected, is marked
-- UNTRUSTED, and gets an OPEN defect row here with the evidence the modelling-agent/lead works. Idempotent
-- upsert keyed on (feature, version): re-running a day refreshes the evidence + last_seen, never dupes.
CREATE TABLE IF NOT EXISTS feature_parity_defect (
    feature          text   NOT NULL,
    version          text   NOT NULL,
    feature_group    text,                              -- the feature's group (for triage by family)
    status           text   NOT NULL DEFAULT 'open',    -- open | investigating | fixed | wontfix | auto_closed
    first_seen_day   date   NOT NULL,                   -- first CLEAN day this feature failed parity
    last_seen_day    date   NOT NULL,                   -- most recent CLEAN day it failed
    clean_days_failed int   NOT NULL DEFAULT 0,         -- # clean days with a parity failure
    clean_streak     int    NOT NULL DEFAULT 0,         -- # consecutive CLEAN recurrence-free DAYS (auto-close fuel)
    last_streak_day  date,                              -- the clean day that last advanced clean_streak (per-day idempotency)
    worst_rel_err    double precision,                  -- worst relative error across exemplar cells
    -- a few exemplar diverging cells pulled from feature_validation_exception, as JSON:
    -- [{"symbol","ts","stream_value","backfill_value","rel_err"}, ...]
    exemplars        jsonb,
    opened_at        timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature, version)
);
-- Additive for an already-provisioned cluster (this init script only runs on a FRESH DB): the AUTO-CLOSE
-- streak column. The defect backlog is otherwise UPSERT/open-only, so a since-fixed / transient divergence
-- would stay 'open' forever and rot trust%. A defect that grades CLEAN (no recurrence) on a CLEAN settled
-- sweep increments clean_streak; at AUTO_CLOSE_STREAK consecutive clean sweeps it auto-closes
-- (status='auto_closed', kept DISTINCT from a manual 'fixed' so provenance is clear). A genuine recurrence
-- resets the streak to 0 and re-opens it.
ALTER TABLE feature_parity_defect
    ADD COLUMN IF NOT EXISTS clean_streak int NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_streak_day date;

CREATE INDEX IF NOT EXISTS idx_fpd_status ON feature_parity_defect (status);
CREATE INDEX IF NOT EXISTS idx_fpd_last_seen ON feature_parity_defect (last_seen_day);

-- Per-(symbol, day) cleanliness verdict — the audit trail behind WHY a symbol-day was excluded from a
-- feature's clean grade (so an operator can see "AAPL on 2026-06-13 was contaminated: internal_gap").
-- Small (one row per collected symbol per swept day) and queryable; idempotent per (symbol, day).
CREATE TABLE IF NOT EXISTS stream_symbol_day_cleanliness (
    symbol             text   NOT NULL,
    day                date   NOT NULL,
    n_stream_minutes   int    NOT NULL,
    n_backfill_minutes int    NOT NULL,
    coverage_frac      double precision,
    max_gap_minutes    int,
    is_clean           boolean NOT NULL,
    reason             text   NOT NULL,                 -- clean | low_coverage | internal_gap
    recorded_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, day)
);
CREATE INDEX IF NOT EXISTS idx_ssdc_day ON stream_symbol_day_cleanliness (day);
