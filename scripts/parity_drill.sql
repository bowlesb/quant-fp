-- backfill<->realtime BAR parity drill (QA task #14).
--
-- Characterizes the stream-vs-backfill close-price disagreement that the
-- `backfill_realtime_parity` invariant flags. Run:
--   docker compose exec -T timescaledb psql -U quant -d quant -f - < scripts/parity_drill.sql
--
-- FINDINGS (2026-06-12, 678,288 overlap bars, 1.14% mismatch >0.2%):
--   * Driver 1 (~11%): KLAC stream close is a persistent EXACTLY-10x the backfill close
--     (e.g. 2312.47 vs 231.01) across BOTH settled days -> a standing stream feed scaling/
--     decimal bug for KLAC, NOT a split-date artifact. All 833 KLAC overlap bars, the entire
--     >10% band. Discrete, fixable. CHECK other symbols for similar Nx ratios.
--   * Driver 2 (~87%): ~15-20 symbols (SPYM, ALB, WMB, GPN, NDAQ, CB, AMT, ADP, BR, DKS, ...)
--     mismatch 100% of their bars but SMALL (<1%, the 0.2-1% bands). A consistent per-symbol
--     offset across every bar = a methodological close-price difference (consolidated/official
--     close vs last-trade minute close), not random tick loss.
--   * Time: mismatch rises through the day, peaks in RTH afternoon (~1.3-1.4%); NOT an
--     extended-hours boundary effect. Concentrated in 189 symbol-days.
-- Owner of the FIX: prod-architect (ingestion). Gate stays at 1% (catches real issues); even
-- after removing KLAC the residual ~1.02% is the per-symbol offset, pending a canonical-close
-- decision.

\timing off
CREATE TEMP TABLE mm AS
WITH s AS (SELECT symbol, ts, close FROM bars_1m WHERE source='stream'),
bnds AS (SELECT min(ts) AS mn, max(ts) AS mx FROM s)
SELECT s.symbol, s.ts, s.close AS stream_close, b.close AS backfill_close,
       abs(s.close-b.close)/abs(b.close) AS rel_diff
FROM s JOIN bars_1m b ON b.symbol=s.symbol AND b.ts=s.ts AND b.source='backfill'
WHERE b.ts >= (SELECT mn FROM bnds) AND b.ts <= (SELECT mx FROM bnds) AND b.close <> 0;

\echo '== A. overall =='
SELECT count(*) AS overlap, count(*) FILTER (WHERE rel_diff>0.002) AS mismatch,
       round(100.0*count(*) FILTER (WHERE rel_diff>0.002)/count(*),2) AS pct,
       count(DISTINCT symbol) AS symbols, min(ts::date) AS from_d, max(ts::date) AS to_d
FROM mm;

\echo '== B. top symbols by mismatch count (100%-mismatch + magnitude) =='
SELECT symbol, count(*) FILTER (WHERE rel_diff>0.002) AS mm, count(*) AS tot,
       round(100.0*count(*) FILTER (WHERE rel_diff>0.002)/count(*),1) AS pct,
       round(max(rel_diff)::numeric,4) AS maxdiff
FROM mm GROUP BY symbol HAVING count(*) FILTER (WHERE rel_diff>0.002)>0
ORDER BY mm DESC LIMIT 20;

\echo '== C. mismatch by date =='
SELECT ts::date AS d, count(*) FILTER (WHERE rel_diff>0.002) AS mm, count(*) AS tot
FROM mm GROUP BY 1 ORDER BY 1;

\echo '== D. mismatch by ET hour =='
SELECT extract(hour from (ts AT TIME ZONE 'America/New_York')) AS et_hr,
       count(*) FILTER (WHERE rel_diff>0.002) AS mm, count(*) AS tot,
       round(100.0*count(*) FILTER (WHERE rel_diff>0.002)/NULLIF(count(*),0),2) AS pct
FROM mm GROUP BY 1 ORDER BY 1;

\echo '== E. magnitude distribution of mismatches =='
SELECT CASE WHEN rel_diff<0.005 THEN '0.2-0.5%' WHEN rel_diff<0.01 THEN '0.5-1%'
            WHEN rel_diff<0.02 THEN '1-2%' WHEN rel_diff<0.05 THEN '2-5%'
            WHEN rel_diff<0.10 THEN '5-10%' ELSE '>10%' END AS band, count(*)
FROM mm WHERE rel_diff>0.002 GROUP BY 1 ORDER BY 1;

\echo '== F. large mismatches (rel_diff>0.05): Nx scaling / split-adjust suspects =='
SELECT symbol, ts, round(stream_close::numeric,2) AS strm, round(backfill_close::numeric,2) AS bf,
       round((stream_close/backfill_close)::numeric,3) AS ratio, round(rel_diff::numeric,3) AS rdiff
FROM mm WHERE rel_diff>0.05 ORDER BY rel_diff DESC LIMIT 30;

\echo '== G. concentration: how many symbol-days carry any mismatch =='
SELECT count(*) AS symbol_days_with_mm FROM (
  SELECT symbol, ts::date FROM mm WHERE rel_diff>0.002 GROUP BY symbol, ts::date
) sd;
