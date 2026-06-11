-- Data sanity probe battery. Run regularly (psql -f) while collecting data.
-- This is a GROWING battery, not a fixed gate: each loop cycle, run it AND invent
-- new angles to look at the data. "A few checks passed" is never "the data is OK."
-- Anomalies -> investigate + log in JOURNAL.md.

\echo '== 1. Integrity invariants (expect all 0) =='
SELECT 'bars: high<low' AS probe, count(*) FROM bars_1m WHERE high < low
UNION ALL SELECT 'bars: high<open/close', count(*) FROM bars_1m WHERE high < open OR high < close
UNION ALL SELECT 'bars: low>open/close',  count(*) FROM bars_1m WHERE low > open OR low > close
UNION ALL SELECT 'bars: close<=0',        count(*) FROM bars_1m WHERE close <= 0
UNION ALL SELECT 'bars: volume<0',        count(*) FROM bars_1m WHERE volume < 0
UNION ALL SELECT 'bars: vwap outside [low,high]', count(*) FROM bars_1m WHERE vwap IS NOT NULL AND (vwap<low-0.01 OR vwap>high+0.01)
UNION ALL SELECT 'bars: ts off minute-grid', count(*) FROM bars_1m WHERE extract(second from ts)<>0
UNION ALL SELECT 'trade_agg: imbalance out of [-1,1]', count(*) FROM trade_agg_1m WHERE signed_volume/NULLIF(buy_volume+sell_volume,0) NOT BETWEEN -1 AND 1
UNION ALL SELECT 'quote_agg: imbalance out of [-1,1]', count(*) FROM quote_agg_1m WHERE quote_imbalance NOT BETWEEN -1 AND 1
UNION ALL SELECT 'quote_agg: spread<0 or >1000bps', count(*) FROM quote_agg_1m WHERE mean_spread_bps<0 OR mean_spread_bps>1000;

\echo '== 2. Independent cross-check: bars.trade_count vs our trade_agg.n_trades (stream) =='
SELECT round(corr(b.trade_count,t.n_trades)::numeric,4) AS correlation,
       round(avg(abs(b.trade_count-t.n_trades))::numeric,1) AS mean_abs_diff
FROM bars_1m b JOIN trade_agg_1m t ON t.symbol=b.symbol AND t.ts=b.ts AND t.source='stream'
WHERE b.source='stream';

\echo '== 3. Extreme 1-min returns, split by gap-spanning vs truly consecutive =='
WITH r AS (SELECT symbol, close/NULLIF(lag(close) OVER w,0)-1 AS ret,
                  EXTRACT(EPOCH FROM (ts-lag(ts) OVER w))/60 AS gap_min
           FROM bars_1m WHERE source='backfill' WINDOW w AS (PARTITION BY symbol ORDER BY ts))
SELECT count(*) FILTER (WHERE abs(ret)>0.5 AND gap_min<=1) AS consecutive_gt50pct,
       count(*) FILTER (WHERE abs(ret)>0.5 AND gap_min>1)  AS gap_spanning_gt50pct
FROM r WHERE ret IS NOT NULL;

\echo '== 4. Extended-hours exposure (RTH = 13:30-20:00 UTC) =='
SELECT round(100.0*count(*) FILTER (WHERE NOT (ts::time>='13:30' AND ts::time<'20:00'))/count(*),1) AS pct_extended_hours
FROM bars_1m;

\echo '== 5. Per-day session span (is the first bar really the RTH open?) =='
SELECT ts::date AS d, min(ts::time) AS earliest, max(ts::time) AS latest
FROM bars_1m WHERE source='backfill' GROUP BY 1 ORDER BY 1 DESC LIMIT 5;

\echo '== 6. Backfill calendar coverage: trading days present + bars/day per symbol =='
SELECT count(DISTINCT ts::date) AS distinct_days,
       min(ts::date) AS from_day, max(ts::date) AS to_day
FROM bars_1m WHERE source='backfill';

\echo '== 7. Feature NaN rate + variance (zero std = constant/dead feature) =='
WITH n AS (SELECT names FROM feature_sets WHERE version='v1.0.0')
SELECT n.names[i] AS feature,
       round(100.0*count(*) FILTER (WHERE fv.vector[i]='NaN'::float8)/count(*),1) AS pct_nan,
       round(stddev(NULLIF(fv.vector[i],'NaN'::float8))::numeric,5) AS std
FROM feature_vectors fv, n, generate_subscripts(fv.vector,1) i
WHERE fv.source='historical' GROUP BY n.names[i], i ORDER BY pct_nan DESC;

\echo '== 8. Label centering: cross-sectional excess should be ~median-0 per ts =='
SELECT horizon, round(avg(value)::numeric,6) AS mean, round(stddev(value)::numeric,6) AS std,
       round(min(value)::numeric,4) AS min, round(max(value)::numeric,4) AS max
FROM labels GROUP BY horizon;

\echo '== Feature warmup/coverage: features NaN-degraded in EARLY dates vs late (catches ragged warmup; I4) =='
-- early-NaN >> late-NaN  => a feature lacking enough backfill at the panel start
-- (warmup not covered by pre-window history). A constantly-high NaN feature is dead, not ragged.
WITH d AS (
  SELECT set_version, ts::date AS dt, vector FROM feature_vectors WHERE source='historical'
),
bounds AS (SELECT set_version, min(dt) AS mn, max(dt) AS mx FROM d GROUP BY set_version),
exploded AS (
  SELECT d.set_version, u.idx, (u.val = 'NaN'::float8)::int AS isnan,
         (d.dt <= b.mn + 5) AS early, (d.dt >= b.mx - 5) AS late
  FROM d JOIN bounds b USING (set_version),
       LATERAL unnest(d.vector) WITH ORDINALITY AS u(val, idx)
)
SELECT set_version, idx AS feature_idx,
       round(100.0 * avg(isnan) FILTER (WHERE early), 1) AS early_nan_pct,
       round(100.0 * avg(isnan) FILTER (WHERE late), 1)  AS late_nan_pct
FROM exploded
GROUP BY set_version, idx
HAVING round(100.0 * avg(isnan) FILTER (WHERE early), 1)
     > round(100.0 * avg(isnan) FILTER (WHERE late), 1) + 20
ORDER BY set_version, feature_idx;

\echo '== Backfill depth vs target (catches the "phantom backfill / running!=intended" class) =='
-- The backfill-manager target is BACKFILL_TARGET_DAYS (compose). Oldest bar should be at
-- least ~90% as deep as the target, or the deepening silently isn't running.
SELECT min(ts)::date AS oldest_bar,
       (now()::date - min(ts)::date) AS depth_days,
       count(DISTINCT ts::date) AS trading_days
FROM bars_1m WHERE source='backfill';

\echo '== Cross-section BREADTH per date (thin sections poison the demean; gate the deep rebuild) =='
SELECT ts::date AS d, count(DISTINCT symbol) AS symbols
FROM bars_1m WHERE source='backfill'
  AND (ts AT TIME ZONE 'America/New_York')::time >= '09:30'
  AND (ts AT TIME ZONE 'America/New_York')::time <  '16:00'
GROUP BY ts::date HAVING count(DISTINCT symbol) < 20
ORDER BY d;
