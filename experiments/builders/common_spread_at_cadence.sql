-- research.common_spread_at_cadence — measured half-spread at trading-cadence timestamps.
--
-- Owner: modeller. A shared `common_` building block: per (symbol, ts) half-spread in bps at the
-- RTH 30-min cadence marks, from the live quote_agg_1m feed. Feeds cost-by-liquidity gating
-- (task #5), OFI cost modeling, execution fill-prob calibration, and any liquidity-conditioned
-- research. A VIEW (not a snapshot) so it auto-extends as the 50->500-name capture grows.
--
-- PIT notes: quote_agg_1m is the realtime stream aggregated per minute; median_spread_bps is the
-- intra-minute median quoted spread. half_spread_bps = median_spread_bps/2 = the realistic one-way
-- crossing cost. We restrict to RTH 10:00-15:30 ET cadence marks (:00/:30) to match the panel's
-- 30-min trading cadence and exclude the open/close auction spread blow-ups. No lookahead: each row
-- uses only that minute's own quote aggregate.
--
-- Refresh policy: live view (no materialization) — always reflects current quote_agg_1m.

CREATE OR REPLACE VIEW research.common_spread_at_cadence AS
SELECT
    symbol,
    ts,
    median_spread_bps,
    median_spread_bps / 2.0 AS half_spread_bps,
    mean_spread_bps,
    n_quotes,
    (ts AT TIME ZONE 'America/New_York') AS et
FROM quote_agg_1m
WHERE median_spread_bps IS NOT NULL
  AND EXTRACT(minute FROM (ts AT TIME ZONE 'America/New_York'))::int IN (0, 30)
  AND (ts AT TIME ZONE 'America/New_York')::time >= TIME '10:00'
  AND (ts AT TIME ZONE 'America/New_York')::time <= TIME '15:30';

INSERT INTO research.catalog
    (table_name, owner_agent, purpose, builder_script, source_tables, pit_notes, refresh_policy, status)
VALUES (
    'common_spread_at_cadence',
    'modeller',
    'Per-(symbol,ts) half-spread (bps) at RTH 30-min cadence marks — measured one-way trading '
        'cost for cost-by-liquidity gating, OFI cost modeling, and execution fill-prob calibration. '
        'Key Phase-1 finding (task #5): 11/50 liquid equities clear the 1.4bps breakeven; the '
        'ret_5m+position signal is ABSENT on the liquid-50 tier (IC -0.0035 vs +0.023 full panel).',
    'experiments/builders/common_spread_at_cadence.sql',
    ARRAY['public.quote_agg_1m'],
    'half_spread_bps = median_spread_bps/2 of each minute''s own quote aggregate; RTH 10:00-15:30 ET '
        'cadence marks only (excludes auction spread blow-ups). No lookahead — per-minute, self-contained.',
    'live view (always current with quote_agg_1m)',
    'active'
)
ON CONFLICT (table_name) DO UPDATE SET
    purpose = EXCLUDED.purpose,
    pit_notes = EXCLUDED.pit_notes,
    refresh_policy = EXCLUDED.refresh_policy,
    status = 'active';
