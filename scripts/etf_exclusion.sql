-- ETF / leveraged-fund contamination of the rankable universe.
--
-- FINDING (2026-06-11, overnight): ~207 of the 1000 universe_membership "members" (~21%)
-- are ETFs / ETNs / leveraged-inverse funds / commodity pools, NOT single-name equities.
-- They reached the feature panel (1.59M feature_vector rows across 207 ETF symbols) and were
-- ranked cross-sectionally against stocks -- including 3x/-3x and VIX-futures products
-- (SOXL, TQQQ, SQQQ, TNA, UVXY, VXX, UPRO, SPXU, SPXS, TSLL, TSLQ ...). This contaminated the
-- price-only cross-sectional edge test: the "no edge" verdict was drawn on a ~21%-polluted
-- cross-section and must be RE-RUN on a clean equity universe before it can be trusted.
--
-- CLASSIFIER: fund-sponsor names + ETF/ETN keyword. Sponsors never overlap with operating
-- companies, so this is high-precision (keeps Abbott Laboratories / Toronto-Dominion Bank /
-- Equity Residential / ADRs like ARM) and high-recall (catches QQQ "Invesco QQQ Trust",
-- TQQQ "ProShares...", GLD "SPDR Gold...", SOXL "Direxion..." which lack the literal word ETF).
-- Residual slip-through: commodity pools named "United States ... Fund, LP" (e.g. USO) -- the
-- second predicate sweeps those. A curated list should still be eyeballed before the prod cut.

\set etf_re '\\yETF\\y|\\yETN\\y|ProShares|iShares|SPDR|Invesco|Direxion|VanEck|GraniteShares|Tradr|Global X|First Trust|WisdomTree|Vanguard|T-Rex|Roundhill|Defiance|YieldMax|Simplify|Avantis|State Street|ARK Innovation|Janus Henderson|Pacer'

-- 1. How many members are funds (the contamination count)?
SELECT count(*) AS fund_members
FROM universe_membership um JOIN asset_metadata am USING (symbol)
WHERE um.trade_date = (SELECT max(trade_date) FROM universe_membership)
  AND um.in_universe
  AND (am.name ~* :'etf_re' OR am.name ~* 'United States .* Fund');

-- 2. The exclusion set (review before acting).
SELECT um.symbol, am.name
FROM universe_membership um JOIN asset_metadata am USING (symbol)
WHERE um.trade_date = (SELECT max(trade_date) FROM universe_membership)
  AND um.in_universe
  AND (am.name ~* :'etf_re' OR am.name ~* 'United States .* Fund')
ORDER BY um.symbol;

-- 3. SUPERVISED universe cleanup (DO NOT run unattended -- changes the panel). After this,
--    re-run the price-only cost-gated battery on the clean equity cross-section.
-- UPDATE universe_membership um SET in_universe = false
-- FROM asset_metadata am WHERE am.symbol = um.symbol AND um.in_universe
--   AND (am.name ~* :'etf_re' OR am.name ~* 'United States .* Fund');

-- 4. Clean equity-only liquidity-ranked scaling list (order-flow rollout order).
SELECT string_agg(symbol, ',' ORDER BY dv DESC) AS scale_list
FROM (
  SELECT b.symbol, sum(b.close * b.volume) AS dv
  FROM bars_1m b
  JOIN universe_membership um
    ON um.symbol = b.symbol
   AND um.trade_date = (SELECT max(trade_date) FROM universe_membership)
   AND um.in_universe
  JOIN asset_metadata am ON am.symbol = b.symbol
  WHERE b.ts::date = (SELECT max(ts::date) FROM bars_1m WHERE source IN ('stream','backfill'))
    AND b.source IN ('stream','backfill')
    AND (b.ts AT TIME ZONE 'America/New_York')::time >= '09:30'
    AND (b.ts AT TIME ZONE 'America/New_York')::time <  '16:00'
    AND am.name !~* :'etf_re'
    AND am.name !~* 'United States .* Fund'
  GROUP BY b.symbol
  ORDER BY dv DESC
  LIMIT 200
) ranked;
