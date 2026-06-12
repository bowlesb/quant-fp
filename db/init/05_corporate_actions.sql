-- Authoritative corporate-action feed (Alpaca CorporateActionsClient — task #18).
-- WHY: the backfill-manager walks month-windows at different wall-clock times, so a split
-- landing mid-backfill mixes adjustment states within ONE symbol's series (KLAC 10:1 ex-6/12 →
-- 10x discontinuity). With an authoritative split/dividend source we (a) self-gate QA's jump
-- invariant against REAL actions, (b) trigger a full-history re-fetch on any new action so months
-- never mix bases (#17), (c) expose ex-date exposure so the executor excludes names whose series
-- isn't yet verified consistent. One row per (symbol, action_type, ex_date). Low-volume dimension
-- table (not a hypertable). cash_rate holds the dividend/cash-merger amount; old_rate/new_rate the
-- split ratio (new_rate/old_rate = forward factor). raw keeps the full API payload for fields we
-- don't promote to columns.
CREATE TABLE IF NOT EXISTS corporate_actions (
    symbol       text        NOT NULL,
    action_type  text        NOT NULL,
    ex_date      date        NOT NULL,
    old_rate     numeric,
    new_rate     numeric,
    cash_rate    numeric,
    record_date  date,
    payable_date date,
    raw          jsonb,
    ingested_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, action_type, ex_date)
);

-- Executor ex-date guard + backfill re-fetch trigger both scan recent actions by date.
CREATE INDEX IF NOT EXISTS corporate_actions_ex_date_idx ON corporate_actions (ex_date);

-- PIT-correct consumer view for Family A ex-div/CA features (Modeller-2's #18 consumer spec).
-- Normalized shape keyed (symbol, ex_date): clean action_type, per-share cash_amount, split_ratio
-- (forward factor = new_rate/old_rate), and a BEST-EFFORT announcement_date pulled from the raw
-- payload (Alpaca's declaration/process date — may be NULL; a missing key degrades to NULL, which the
-- consumer accepts and falls back to ex_date for realized flags). ex_date is a calendar fact → no
-- live/backfill skew. PIT DISCIPLINE IS THE CONSUMER'S: at a feature-compute cadence ts, JOIN this
-- view at compute time (like sector_map, NOT baked into feature_vectors) and reveal only rows with
-- announcement_date <= ts (anticipation features) or ex_date <= ts (realized flags) — never let the
-- model see a dividend before it's announced/ex. (announcement_date field name/format to be verified
-- against a live CA payload at populate-time; view is CREATE OR REPLACE so refining it is free.)
CREATE OR REPLACE VIEW corporate_actions_pit AS
SELECT
    symbol,
    ex_date,
    CASE
        WHEN action_type = 'cash_dividends' THEN 'cash_dividend'
        WHEN action_type IN ('forward_splits', 'reverse_splits', 'unit_splits') THEN 'split'
        ELSE action_type
    END AS action_type,
    cash_rate AS cash_amount,                                       -- per-share $, NULL for splits
    CASE WHEN old_rate > 0 THEN new_rate / old_rate END AS split_ratio,  -- forward factor, NULL for dividends
    COALESCE((raw ->> 'declaration_date')::date, (raw ->> 'process_date')::date) AS announcement_date,
    record_date,
    payable_date
FROM corporate_actions;
