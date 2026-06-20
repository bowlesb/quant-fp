-- Within-day parity certification ledger (docs/WITHIN_DAY_PARITY_CERTIFICATION.md). The per-feature,
-- per-day stamp the Within-Day Parity Certifier (WDPC) writes when it has reviewed a feature's live==
-- backfill match on the settled intraday window and there is nothing left to do on it today. This is a
-- daily OPERATIONAL checkpoint ("reviewed, nothing outstanding"), DISTINCT from the permanent binary
-- feature_trust.trust_state grant — a 'certified' row at the feature's tolerance + min_pass_rate is the
-- evidence the existing binary-trust grant path consumes (trust_reason='within_day_parity'), so this
-- ledger FEEDS trust, it does not duplicate it. Idempotent: re-runnable on the live DB.

CREATE TABLE IF NOT EXISTS within_day_parity_cert (
    feature         text        NOT NULL,
    version         text        NOT NULL,     -- (feature, version) like the rest of the trust model
    group_name      text        NOT NULL,     -- the compute/version unit parity is earned over
    cert_day        date        NOT NULL,     -- the day the within-day review happened
    status          text        NOT NULL,     -- 'certified' | 'fix_pending' | 'defected'
                                              --            | 'skipped_unsettled' | 'skipped_contaminated'
    stable_cycles   int         NOT NULL DEFAULT 0,  -- consecutive clean settled-window comparisons achieved
    window_minutes  int         NOT NULL DEFAULT 0,  -- contiguous settled minutes the match held over
    value_rate      double precision,         -- WORST per-cycle match rate across the stable run
    n_clean_symbols int,                       -- breadth of the evidence (sampled clean symbols)
    n_compared      bigint      NOT NULL DEFAULT 0,  -- compared cells behind the verdict
    tolerance       double precision,         -- rtol the match was judged at (provenance / replay)
    min_pass_rate   double precision,         -- threshold required to certify
    settle_lag_min  double precision,         -- the SETTLE_LAG (minutes) the window was held back by
    git_commit      text,                      -- code the LIVE side ran (provenance)
    content_hash    text,                      -- the group's content hash at cert time
    reason          text,                      -- human note (esp. for skipped/fix_pending/defected)
    certified_at    timestamptz NOT NULL DEFAULT now(),
    -- One stamp per (feature, version) per day; a re-review the same day UPSERTs in place (idempotent).
    PRIMARY KEY (feature, version, cert_day)
);

CREATE INDEX IF NOT EXISTS idx_wdpc_day    ON within_day_parity_cert (cert_day);
CREATE INDEX IF NOT EXISTS idx_wdpc_status ON within_day_parity_cert (status);
CREATE INDEX IF NOT EXISTS idx_wdpc_group  ON within_day_parity_cert (group_name);

COMMENT ON TABLE within_day_parity_cert IS
    'Within-day parity certification stamps (docs/WITHIN_DAY_PARITY_CERTIFICATION.md). One row per '
    '(feature,version,cert_day). status=certified asserts: on cert_day, this version emitted LIVE values '
    'that matched its BACKFILL recomputation on the recently-settled intraday window, within tolerance on '
    '>=min_pass_rate of n_clean_symbols clean symbols, held stable for stable_cycles cycles over '
    'window_minutes contiguous settled minutes — nothing left to do on this feature today. A certified row '
    'is the evidence the binary-trust grant consumes (trust_reason=within_day_parity); the nightly sweep is '
    'the backstop (a within-day cert the nightly later contradicts reopens a defect via #161).';

-- Per-day roll-up of the within-day review (operational visibility: how much got reviewed today, and what
-- still has work). Mirrors feature_trust_summary's shape.
DROP VIEW IF EXISTS within_day_parity_summary;
CREATE VIEW within_day_parity_summary AS
SELECT
    cert_day,
    status,
    count(*)                                 AS n_features,
    round(avg(value_rate)::numeric, 5)       AS avg_value_rate,
    round(avg(stable_cycles)::numeric, 2)    AS avg_stable_cycles
FROM within_day_parity_cert
GROUP BY cert_day, status;
