-- Settled-day TRADE-AGG parity proof at current order-flow scale (QA task #15 / ROADMAP I2b).
--
-- Order flow is THE designated edge candidate (M2/M3). Before committing to 500-name sharded
-- ingestion, prove our live trade-aggregation matches the complete REST record on a SETTLED
-- day at the current ~52-name scale — a failure here redirects M2 before we build sharding.
-- Run (edit :day to the settled day under test):
--   docker compose exec -T timescaledb psql -U quant -d quant -f - < scripts/trade_agg_parity_settled.sql
--
-- FINDINGS (settled day 2026-06-11, 50 symbols, 6,058 overlap minutes):
--   GREEN (the core RTH aggregation is trustworthy):
--     * n_trades within 2%: 98.05% (corr 0.9997) — clears the 98% gate.
--     * tick-rule SIGN agreement: 99.82%; imbalance within 0.05: 99.75% — the HARDEST threat
--       (sign depends on order + last-price state, sensitive to out-of-order live delivery) is solid.
--     * signed_volume within 2% of volume: 99.34%.
--   CAVEATS to clear before scaling (real, found at 52 names):
--     * CLOSE-HOUR COLLAPSE: 16:00 ET hour n_trades-within-2% = 14% (15:00 hr = 93%). Closing-
--       cross / late-and-out-of-sequence prints diverge live-vs-REST -> OFI features at/after the
--       close are NOT trustworthy. Minute-boundary state init (I2b threat #4) is the suspect.
--     * COVERAGE MISMATCH: backfill 34,784 min vs stream 18,860 min; 28,726 backfill-only
--       (stream is RTH-concentrated, ~363 vs ~682 min/symbol) and 12,802 stream-only minutes.
--       The minute-set mismatch must be understood before trusting at scale.
--   VERDICT: core RTH count+sign parity passes at 52 names; gate OFI on (a) excluding the
--   closing minutes until close-hour parity is fixed and (b) explaining the coverage mismatch.
--   Owner of the at-scale DATA PATH: prod-architect; QA re-runs this as scale grows.

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
