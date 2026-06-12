-- Settled-day TRADE-AGG parity proof at current order-flow scale (QA task #15 / ROADMAP I2b).
--
-- Order flow is THE designated edge candidate (M2/M3). Before committing to 500-name sharded
-- ingestion, prove our live trade-aggregation matches the complete REST record on a SETTLED
-- day at the current ~52-name scale — a failure here redirects M2 before we build sharding.
-- Run (edit :day to the settled day under test):
--   docker compose exec -T timescaledb psql -U quant -d quant -f - < scripts/trade_agg_parity_settled.sql
--
-- FINDINGS (settled day 2026-06-11):
--   *** THE HEADLINE NUMBER IS NOT AN AT-SCALE PROOF. *** The per-minute coverage drill (sections
--   below) shows the live STREAM captured only ~10 of 50 names for the WHOLE day until ~15:51 ET,
--   when the subscription scaled 10->50; backfill had all 50 throughout. So the 6,058 "overlap"
--   minutes and the 98.05% within-2% / 99.82% sign agreement are essentially a 10-NAME proof plus
--   a ~10-minute 50-name window (15:51-16:00). A true 50-name full-session proof needs a day with
--   all 50 streamed start-to-finish (earliest 2026-06-12).
--   GOOD news where the stream DID capture a name:
--     * per-minute parity excellent — 15:30 / 15:45 / 15:55 ET all 100% count + sign (the overnight
--       label anchor + last intraday cadence are CLEAN; the overnight verdict is not tainted).
--     * tick-rule SIGN agreement 99.82% — the HARDEST threat (sign depends on order+last-price
--       state, sensitive to out-of-order live delivery) holds on the captured names.
--   RESIDUAL THREATS:
--     * 16:00-ET CLOSING-PRINT minute = 14% within-2% (closing-auction divergence) -> exclude
--       >=16:00; the Modeller's conservative >=15:50 OFI line is safe (15:50-15:59 ~100%).
--     * backfill trade-agg is RTH-bounded (no post-16:00 ET) -> post-close OFI has NO backfill to
--       validate against at all.
--   VERDICT: encouraging on the 10 streamed names, but NOT proven at 50. Re-run on the first full
--   50-name settled session. Owner of the 10->50 live-coverage gap + at-scale data path: prod-architect.

\timing off
\set day '2026-06-11'

\echo '== COVERAGE: minutes per source (dropped-minute threat) =='
WITH st AS (SELECT symbol, ts FROM trade_agg_1m WHERE source='stream'   AND ts::date=:'day'),
     bf AS (SELECT symbol, ts FROM trade_agg_1m WHERE source='backfill' AND ts::date=:'day')
SELECT (SELECT count(*) FROM bf) AS backfill_min, (SELECT count(*) FROM st) AS stream_min,
       (SELECT count(*) FROM bf JOIN st USING(symbol,ts)) AS overlap_min,
       (SELECT count(*) FROM bf LEFT JOIN st USING(symbol,ts) WHERE st.ts IS NULL) AS in_bf_not_stream,
       (SELECT count(*) FROM st LEFT JOIN bf USING(symbol,ts) WHERE bf.ts IS NULL) AS in_stream_not_bf;

CREATE TEMP TABLE j AS
SELECT s.symbol, s.ts, s.n_trades AS s_nt, b.n_trades AS b_nt,
       s.signed_volume AS s_sv, b.signed_volume AS b_sv,
       (s.buy_volume+s.sell_volume) AS s_tot, (b.buy_volume+b.sell_volume) AS b_tot,
       s.signed_volume/NULLIF(s.buy_volume+s.sell_volume,0) AS s_imb,
       b.signed_volume/NULLIF(b.buy_volume+b.sell_volume,0) AS b_imb
FROM trade_agg_1m s JOIN trade_agg_1m b ON b.symbol=s.symbol AND b.ts=s.ts AND b.source='backfill'
WHERE s.source='stream' AND s.ts::date=:'day';

\echo '== 1. n_trades parity (count) within 2% =='
SELECT count(*) AS overlap,
       round(100.0*count(*) FILTER (WHERE b_nt<>0 AND abs(s_nt-b_nt)/abs(b_nt::float)<=0.02)/count(*),2) AS within_2pct,
       round(corr(s_nt,b_nt)::numeric,4) AS corr, round(avg(abs(s_nt-b_nt))::numeric,2) AS mean_abs_diff
FROM j;

\echo '== 2. IMBALANCE / tick-rule SIGN parity (the hard threat) =='
SELECT round(100.0*count(*) FILTER (WHERE abs(s_imb-b_imb)<=0.05)/count(*),2) AS imb_within_0_05,
       round(100.0*count(*) FILTER (WHERE sign(s_imb)=sign(b_imb))/count(*),2) AS sign_agree_pct,
       round(avg(abs(s_imb-b_imb))::numeric,4) AS mean_abs_imb_diff
FROM j WHERE s_imb IS NOT NULL AND b_imb IS NOT NULL;

\echo '== 3. signed_volume within 2% of total volume =='
SELECT round(100.0*count(*) FILTER (WHERE b_tot<>0 AND abs(s_sv-b_sv)/abs(b_tot)<=0.02)/count(*),2) AS sv_within_2pct_of_vol FROM j;

\echo '== 4. by ET hour: where count-parity breaks (close-hour collapse) =='
SELECT extract(hour from (ts AT TIME ZONE 'America/New_York')) AS et_hr, count(*) AS min,
       round(100.0*count(*) FILTER (WHERE b_nt<>0 AND abs(s_nt-b_nt)/abs(b_nt::float)<=0.02)/count(*),1) AS nt_within2
FROM j GROUP BY 1 ORDER BY 1;

\echo '== 5. SYMBOL COVERAGE per source by ET hour (THE at-scale check: is the stream really 50?) =='
SELECT extract(hour from (ts AT TIME ZONE 'America/New_York')) AS et_hr,
       count(DISTINCT symbol) FILTER (WHERE source='stream')   AS stream_syms,
       count(DISTINCT symbol) FILTER (WHERE source='backfill') AS backfill_syms
FROM trade_agg_1m WHERE ts::date=:'day' GROUP BY 1 ORDER BY 1;

\echo '== 6. per-minute parity, closing hour 15:00-16:00 ET (overnight anchor 15:30; MOC line) =='
SELECT to_char((ts AT TIME ZONE 'America/New_York')::time,'HH24:MI') AS et_min, count(*) AS syms,
       round(100.0*count(*) FILTER (WHERE b_nt<>0 AND abs(s_nt-b_nt)/abs(b_nt::float)<=0.02)/count(*),1) AS nt_within2,
       round(100.0*count(*) FILTER (WHERE s_imb IS NOT NULL AND b_imb IS NOT NULL AND sign(s_imb)=sign(b_imb))/NULLIF(count(*) FILTER (WHERE s_imb IS NOT NULL AND b_imb IS NOT NULL),0),1) AS sign_agree
FROM j WHERE (ts AT TIME ZONE 'America/New_York')::time >= '15:00'
        AND (ts AT TIME ZONE 'America/New_York')::time < '16:00'
GROUP BY 1 ORDER BY 1;
