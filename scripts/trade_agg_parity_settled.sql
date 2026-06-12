-- Settled-day TRADE-AGG parity proof at current order-flow scale (QA task #15 / ROADMAP I2b).
--
-- Order flow is THE designated edge candidate (M2/M3). Before committing to 500-name sharded
-- ingestion, prove our live trade-aggregation matches the complete REST record on a SETTLED
-- day at the current ~52-name scale — a failure here redirects M2 before we build sharding.
-- Run (edit :day to the settled day under test):
--   docker compose exec -T timescaledb psql -U quant -d quant -f - < scripts/trade_agg_parity_settled.sql
--
-- *** FINDINGS (settled day 2026-06-12) — FIRST TRUE FULL-50 AT-SCALE PROOF: PASS. ***
--   COVERAGE is the headline: stream==backfill 50 names EVERY hour 04:00-16:00 ET (36,334 overlap
--   minutes) — a genuine full-session 50-name proof, NOT the 6/11 10-name proxy. Vs the >=98% gate:
--     * n_trades within-2% = 99.79% (corr 1.0000, mean abs diff 0.11)
--     * tick-rule SIGN agreement = 99.85% — the HARDEST threat (sign depends on order+last-price
--       state, sensitive to out-of-order live delivery) HOLDS at scale.
--     * signed_volume within-2%-of-vol = 99.41%.
--   By hour: 100% all premarket+RTH; only the 16:00 closing hour dips to 95.5% (closing-auction).
--   Section 6: 15:50-15:59 all 100% count / 100% sign -> Modeller's >=15:50 OFI line is CLEAN.
--   => M2 exit criterion "settled-day I2b >=98% at scale" MET; green light for 500-name scaling.
--   STANDING residual (by design, unchanged): backfill trade-agg is RTH-bounded -> post-16:00 OFI
--   has NO backfill to validate against; keep OFI <=15:59 ET. Re-prove as scale grows 50->500.
--
-- HISTORY (settled day 2026-06-11): NOT an at-scale proof — the stream captured only ~10 of 50
--   names until ~15:51 ET (the 7dfb438 deploy-restart one-off, since resolved); headline 98.05% /
--   sign 99.82% was a 10-name proof + a ~10-min 50-name window. Superseded by the 6/12 full-50 run.

\timing off
\set day '2026-06-12'

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
