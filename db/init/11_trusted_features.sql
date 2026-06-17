-- TRUSTED-FEATURES surface — the queryable SELECTION that triggers the modeling pipeline.
--
-- Ben's directive: "for features showing signs of trustability, backfill them so we can build good
-- lightGBM models on our carefully-selected features." A feature's TRUST grade is the gate: once it has
-- earned trust (parity held across enough CLEAN regular-session days), it enters the model pipeline —
-- the backfill agent selectively backfills the trusted features over the 378d window and the modelling
-- agent trains lightGBM on the growing trusted set. So this surface must answer, in one place:
--
--   "Which features have EARNED trust (and may be backfilled + modelled), as of now?"
--
-- It is a thin VIEW over feature_trust.lifecycle_state (the contamination-aware grade — see
-- docs/TRUST_METADATA.md), NOT a new source of truth. The trust state machine (quantlib.features.
-- trust_lifecycle) remains the only writer; this just exposes it cleanly so downstream code does not have
-- to re-encode "what counts as trusted". Definition of trusted == VALIDATED:
--   lifecycle_state = 'VALIDATED'  <=>  >= MIN_CLEAN_DAYS (2) clean days AND parity held every clean day
--                                       (clean_value_rate >= 0.999). PENDING/DIVERGENT/RETIRED are NOT
--                                       trusted (not-yet-proven / quarantined / retired).

-- The trusted set: one row per trusted (feature, version), with the evidence a consumer may want to rank
-- or sanity-check on (how many clean days, the clean match rate, when it last validated).
CREATE OR REPLACE VIEW trusted_features AS
SELECT
    feature,
    version,
    lifecycle_state,                 -- always 'VALIDATED' here (kept for an explicit, self-documenting column)
    clean_days,                      -- # CLEAN regular-session days parity was compared on (>= 2)
    clean_days_passed,               -- # of those clean days parity held (== clean_days for VALIDATED)
    clean_value_rate,                -- lifetime match rate over CLEAN comparisons (>= 0.999)
    last_validated_day,              -- most recent day this feature was validated
    lifecycle_updated_at             -- when the trust grade was last (re)computed
FROM feature_trust
WHERE lifecycle_state = 'VALIDATED';

-- A coverage roll-up the operator / coordinator can poll for "how big is the trusted cohort": one row per
-- lifecycle_state with counts + the breadth of clean-day evidence behind it. PENDING here = "proven on
-- some clean days but not yet 2" (the next cohort to cross), DIVERGENT = quarantined (kept + flagged).
CREATE OR REPLACE VIEW feature_trust_summary AS
SELECT
    COALESCE(lifecycle_state, 'UNGRADED') AS lifecycle_state,
    count(*)                              AS n_features,
    round(avg(clean_days)::numeric, 2)    AS avg_clean_days,
    round(avg(clean_value_rate)::numeric, 5) AS avg_clean_value_rate
FROM feature_trust
GROUP BY COALESCE(lifecycle_state, 'UNGRADED');

COMMENT ON VIEW trusted_features IS
    'Features that have EARNED trust (lifecycle_state=VALIDATED: parity held on >= 2 clean RTH days). The '
    'SELECTION the backfill agent + modelling agent gate on — backfill + train only these. Grows as the '
    'nightly sweep promotes PENDING -> VALIDATED. Pure view over feature_trust; trust_lifecycle is the writer.';
