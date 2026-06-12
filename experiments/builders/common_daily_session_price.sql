-- research.common_daily_session_price — per-(symbol, trade_date) session anchor prices.
--
-- Owner: explorer-shapes (delivered for the Lead to EXPLAIN/run in a quiet window + register).
-- A shared `common_` building block: the open(09:30) / 10:00 / first-30-min-range / close(15:59)
-- anchors per name per RTH session. Unblocks the open-anchored shape CLASS (gap fade/follow #002,
-- opening-range breakout #004, and gap-conditioning elsewhere) so those shapes are a cheap JOIN
-- instead of re-scanning all 693 bars_1m chunks per experiment (the (ts AT TIME ZONE 'ET')::time
-- IN(...) predicate is non-indexable — pay the full scan ONCE here, not per experiment).
--
-- DESIGN: a MATERIALIZED snapshot (NOT a live view), because the whole point is to avoid the
-- repeated bars_1m scan. One sequential pass builds the table; shapes then read this small table.
-- Refresh: re-run this builder to extend with new sessions (idempotent — DROP+rebuild, or run
-- incrementally with a BACKFILL_START analogue; for now it materializes the full backfill history).
--
-- PRICE SOURCE: bars_1m WHERE source='backfill' (the research basis), RTH only, America/New_York
-- with DST handled by the ::time-in-ET resolution (NOT hardcoded UTC offsets).
--   open_0930       = the 09:30 ET bar's OPEN (the true session open price).
--   px_1000         = the 10:00 ET bar's CLOSE (post-opening-range mark).
--   high/low/vol_0930_1000 = aggregated over the first-30-min window [09:30, 10:00) ET (6 bars
--                     09:30..09:59) — the opening range + its volume.
--   close_1600      = the 15:59 ET bar's CLOSE (the canonical session close; matches the panel's
--                     canonical-close convention — the 16:00 auction minute is NOT used).
--
-- EARLY-CLOSE HONESTY: on early-close days (13:00 ET close) the 15:59 bar is ABSENT — close_1600
-- is then NULL (NOT back-filled with a stale price). Downstream open->close labels must treat NULL
-- close_1600 as "no label this day", not zero. open_0930/range are still valid on early-close days.
--
-- PIT: same-day deterministic prices; no future leakage (forward open->close LABELS built FROM this
-- live in the labels layer, not here). No lookahead within the table itself.

DROP TABLE IF EXISTS research.common_daily_session_price;

CREATE TABLE research.common_daily_session_price AS
WITH rth AS (
    SELECT
        symbol,
        (ts AT TIME ZONE 'America/New_York')::date         AS trade_date,
        (ts AT TIME ZONE 'America/New_York')::time         AS et_time,
        open, high, low, close, volume
    FROM bars_1m
    WHERE source = 'backfill'
      AND (ts AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
      AND (ts AT TIME ZONE 'America/New_York')::time <= TIME '16:00'
)
SELECT
    symbol,
    trade_date,
    -- session open: the 09:30 bar's OPEN
    MAX(open)  FILTER (WHERE et_time = TIME '09:30')                        AS open_0930,
    -- 10:00 mark: the 10:00 bar's CLOSE
    MAX(close) FILTER (WHERE et_time = TIME '10:00')                        AS px_1000,
    -- opening range [09:30, 10:00): high / low / volume of the first 30 minutes
    MAX(high)  FILTER (WHERE et_time >= TIME '09:30' AND et_time < TIME '10:00') AS high_0930_1000,
    MIN(low)   FILTER (WHERE et_time >= TIME '09:30' AND et_time < TIME '10:00') AS low_0930_1000,
    SUM(volume) FILTER (WHERE et_time >= TIME '09:30' AND et_time < TIME '10:00') AS vol_0930_1000,
    -- canonical session close: the 15:59 bar's CLOSE (NULL on early-close days -> honest gap)
    MAX(close) FILTER (WHERE et_time = TIME '15:59')                        AS close_1600
FROM rth
GROUP BY symbol, trade_date;

ALTER TABLE research.common_daily_session_price
    ADD PRIMARY KEY (symbol, trade_date);

CREATE INDEX common_daily_session_price_date_idx
    ON research.common_daily_session_price (trade_date);

INSERT INTO research.catalog
    (table_name, owner_agent, purpose, builder_script, source_tables, pit_notes, refresh_policy, status)
VALUES (
    'common_daily_session_price',
    'explorer-shapes',
    'Per-(symbol, trade_date) RTH session anchors: open_0930, px_1000, first-30-min range '
        '(high/low/vol_0930_1000), and canonical close_1600. Materialized once to unblock the '
        'open-anchored shape class (gap fade/follow #002, opening-range breakout #004, gap '
        'conditioning) — a cheap JOIN instead of re-scanning all 693 bars_1m chunks per experiment '
        '(the ET-time IN-list predicate is non-indexable).',
    'experiments/builders/common_daily_session_price.sql',
    ARRAY['public.bars_1m'],
    'source=backfill, RTH America/New_York (DST via ::time-in-ET). open_0930=09:30 bar OPEN; '
        'px_1000=10:00 bar CLOSE; range over [09:30,10:00); close_1600=15:59 bar CLOSE (canonical, '
        'auction minute excluded). EARLY-CLOSE: 15:59 absent -> close_1600 NULL (not stale-filled). '
        'Same-day prices, no lookahead; forward open->close labels live in the labels layer.',
    'materialized snapshot — re-run builder to extend with new sessions',
    'active'
)
ON CONFLICT (table_name) DO UPDATE SET
    purpose = EXCLUDED.purpose,
    pit_notes = EXCLUDED.pit_notes,
    refresh_policy = EXCLUDED.refresh_policy,
    status = 'active';
