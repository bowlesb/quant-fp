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
