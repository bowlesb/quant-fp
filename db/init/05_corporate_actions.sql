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
