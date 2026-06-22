-- Drop the dead ``news`` table. News is persisted EXCLUSIVELY in /store/news parquet (partitioned by
-- published_date, with an append-only manifest); this table was never read or written by any service or
-- feature loader, and the live news_sentiment feature group reads the parquet tape, not the DB. Removing it
-- (and its indexes, dropped with it) clears schema cruft with no data-path or fingerprint impact.
--
-- Idempotent / re-runnable: IF EXISTS no-ops once the table is gone. On a FRESH init this is a no-op because
-- the CREATE was removed from 01_schema.sql in the same change; on an EXISTING DB it drops the empty table.
DROP TABLE IF EXISTS news;
