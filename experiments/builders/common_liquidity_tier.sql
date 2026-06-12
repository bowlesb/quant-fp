-- research.common_liquidity_tier — per-symbol liquidity tier (dollar-volume quartile).
--
-- Owner: explorer-shapes (delivered for the Lead to EXPLAIN/run + register; DB write).
-- A shared `common_` building block: each symbol's full-history median daily dollar-volume and its
-- quartile (liq_q 1=illiquid .. 4=most-liquid). Matches explorer-data's tiering definition exactly
-- (per-symbol median dollar-volume from bars_1m, ntile 4) so the gap-fade liq2/liq3 targeting is
-- directly comparable to their archaeology (inverted-U: fade strongest at liq2/liq3, weakest at mega-cap).
--
-- WHY MATERIALIZE: the dollar-volume aggregate is a full bars_1m scan — pay it ONCE here, not per
-- experiment (the gap-fade, the ret_5m cost-tier work, and any liquidity-conditioned shape all reuse it).
--
-- PIT NOTE: this is a FULL-HISTORY (not point-in-time) per-symbol liquidity classification — a stable
-- symbol attribute for cohort selection, NOT a tradeable signal. A name's tier uses its whole-sample
-- median dollar-volume; acceptable for tier MEMBERSHIP (liquidity is slow-moving) but NOT to be used as
-- a forward-looking feature. For PIT liquidity, use a trailing-window variant (future extension).
--
-- Refresh: re-run to extend with new symbols / refresh medians.

DROP TABLE IF EXISTS research.common_liquidity_tier;

CREATE TABLE research.common_liquidity_tier AS
WITH daily_dv AS (
    SELECT
        symbol,
        (ts AT TIME ZONE 'America/New_York')::date AS trade_date,
        SUM(close * volume) AS dollar_volume
    FROM bars_1m
    WHERE source = 'backfill'
      AND (ts AT TIME ZONE 'America/New_York')::time >= TIME '09:30'
      AND (ts AT TIME ZONE 'America/New_York')::time <= TIME '16:00'
    GROUP BY symbol, trade_date
),
median_dv AS (
    SELECT
        symbol,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY dollar_volume) AS median_dollar_volume,
        count(*) AS n_days
    FROM daily_dv
    GROUP BY symbol
)
SELECT
    symbol,
    median_dollar_volume,
    n_days,
    ntile(4) OVER (ORDER BY median_dollar_volume) AS liq_q
FROM median_dv;

ALTER TABLE research.common_liquidity_tier ADD PRIMARY KEY (symbol);
CREATE INDEX common_liquidity_tier_q_idx ON research.common_liquidity_tier (liq_q);

INSERT INTO research.catalog
    (table_name, owner_agent, purpose, builder_script, source_tables, pit_notes, refresh_policy, status)
VALUES (
    'common_liquidity_tier',
    'explorer-shapes',
    'Per-symbol full-history median daily dollar-volume + quartile (liq_q 1=illiquid..4=most-liquid). '
        'Matches explorer-data''s tiering for liquidity-conditioned shapes — the gap-fade #002 inverted-U '
        '(fade strongest at liq2/liq3, weakest at mega-cap) and ret_5m cost-tier work. Materialized once '
        '(the dollar-volume aggregate is a full bars_1m scan) so shapes join it cheaply.',
    'experiments/builders/common_liquidity_tier.sql',
    ARRAY['public.bars_1m'],
    'FULL-HISTORY per-symbol median dollar-volume (not PIT) — a stable liquidity ATTRIBUTE for cohort '
        'selection, NOT a tradeable/forward feature. For PIT liquidity use a trailing-window variant.',
    'materialized snapshot — re-run to refresh medians / add symbols',
    'active'
)
ON CONFLICT (table_name) DO UPDATE SET
    purpose = EXCLUDED.purpose,
    pit_notes = EXCLUDED.pit_notes,
    refresh_policy = EXCLUDED.refresh_policy,
    status = 'active';
