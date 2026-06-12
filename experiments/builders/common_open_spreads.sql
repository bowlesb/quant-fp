-- research.common_open_spreads — measured OPEN-MINUTE half-spread, per (symbol, ts).
--
-- Owner: modeller (Research Lead). The open-minute analog of common_spreads_at_cadence, built
-- for the gap-fade candidate's net-of-cost gate: that shape round-trips AT/just-after the 09:30
-- open, which common_spreads_at_cadence excludes (it covers 10:00-15:30 only). The open is the
-- WIDEST-spread window of the day (measured: 09:30 ~25bps, 09:35 ~13bps, 09:40 ~12bps avg spread).
--
-- A VIEW over the early-RTH minutes [09:30, 09:45] ET so any open-anchored shape can join the
-- TRUE open execution cost by (symbol, minute). half_spread_bps = median_spread_bps/2.
--
-- PIT: per-minute own quote aggregate, no lookahead. Refresh: live view (current with quote_agg_1m).
-- COVERAGE CAVEAT: quote_agg_1m is ~50 names / recent days only — so this covers the capture set,
-- not the full panel history. A gap-fade net-of-cost test on the deep panel must apply these
-- measured open spreads as a per-minute COST CURVE (by minute-of-open), not a per-row historical join.

CREATE OR REPLACE VIEW research.common_open_spreads AS
SELECT
    symbol,
    ts,
    (ts AT TIME ZONE 'America/New_York')::time AS et_time,
    median_spread_bps,
    median_spread_bps / 2.0 AS half_spread_bps,
    n_quotes
FROM quote_agg_1m
WHERE median_spread_bps IS NOT NULL
  AND (ts AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
  AND (ts AT TIME ZONE 'America/New_York')::time <= TIME '09:45';

INSERT INTO research.catalog
    (table_name, owner_agent, purpose, builder_script, source_tables, pit_notes, refresh_policy, status)
VALUES (
    'common_open_spreads',
    'modeller',
    'Measured OPEN-MINUTE half-spread (bps) per (symbol, ts) over [09:30,09:45] ET — the TRUE '
        'execution cost for open-anchored shapes (gap-fade #002), which common_spreads_at_cadence '
        '(10:00-15:30) excludes. Open is the widest-spread window: ~25bps@09:30, ~13bps@09:35 avg.',
    'experiments/builders/common_open_spreads.sql',
    ARRAY['public.quote_agg_1m'],
    'half_spread_bps = median_spread_bps/2 of each minute''s own quote aggregate; [09:30,09:45] ET. '
        'No lookahead. COVERAGE: ~50 capture names / recent days — use as a per-minute COST CURVE for '
        'deep-panel gap-fade tests, not a per-row historical join.',
    'live view (current with quote_agg_1m)',
    'active'
)
ON CONFLICT (table_name) DO UPDATE SET
    purpose = EXCLUDED.purpose,
    pit_notes = EXCLUDED.pit_notes,
    status = 'active';
