-- Ledger of monthly bar-backfill windows so the backfill-manager is resumable
-- (don't re-fetch completed months) and its progress is inspectable.
CREATE TABLE IF NOT EXISTS backfill_windows (
    month_start date PRIMARY KEY,
    status      text NOT NULL,            -- 'done' | 'partial'
    bars        bigint,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
