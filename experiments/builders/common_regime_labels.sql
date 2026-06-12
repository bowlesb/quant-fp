-- research.common_regime_labels — per-trade_date PIT regime classification for conditioning.
--
-- Owner: explorer-data. A shared `common_` building block: each RTH trade_date gets a regime
-- label computed STRICTLY from information available BEFORE that day's open, so features/shapes can
-- condition on regime without lookahead. Born from explorer-data journal OBS6: the ret_5m reversal
-- is MONOTONE in calm — strongest in low cross-sectional-dispersion regimes (q1 IC -0.0275) and
-- weakest in high (q5 -0.0150). The gap-fade (OBS7) and any reversal shape want a PIT calm-filter;
-- this table provides it once, for every lens.
--
-- DESIGN: a MATERIALIZED snapshot (~613 rows, one per trade_date). Re-run builder to extend.
--
-- COLUMNS (all PIT — every value for trade_date D uses sessions <= D-1 only):
--   prior_disp     = cross-sectional stddev of the PRIOR session's intraday open->close returns
--                    (how dispersed the market was going INTO today).
--   prior_mkt_ret  = the PRIOR session's equal-weight cross-sectional mean intraday return.
--   trail5_mean    = trailing 5-session mean of the daily cross-sectional mean return, ending at
--                    the prior session.
--   trend_5d       = sign(trail5_mean): +1 up / -1 down / 0 flat.
--   disp_pctile    = percentile rank (0..1) of prior_disp within the TRAILING 120 prior sessions
--                    (PIT — counts only sessions strictly before D). NULL until >=20 trailing exist.
--   disp_tier      = 1..5 from disp_pctile (ceil(pctile*5), clamped). 1=calmest, 5=most volatile.
--                    A "calm filter" = disp_tier <= 3 (or exclude tier 5).
--
-- PIT DISCIPLINE: the percentile uses a self-join over sessions with rn in [D.rn-120, D.rn-1]
-- (strictly prior), so no same-day or future data enters. Quantile is RANK-based (robust to the
-- fat dispersion tail), not min/max width_bucket. Early sessions (<20 prior) get NULL tier —
-- honest, not a fabricated tier.
--
-- SOURCE: research.common_daily_session_price (PIT, source=backfill, RTH, canonical 15:59 close,
-- early-close-honest: NULL close_1600 days drop out of dispersion). Extends the catalog's
-- open-anchor table rather than re-scanning bars_1m — dedup per the pipeline rule.

DROP TABLE IF EXISTS research.common_regime_labels;

CREATE TABLE research.common_regime_labels AS
WITH sess_ret AS (
    SELECT
        trade_date,
        (close_1600 - open_0930) / open_0930 AS intraday_ret
    FROM research.common_daily_session_price
    WHERE close_1600 IS NOT NULL AND open_0930 IS NOT NULL AND open_0930 > 0
),
day_agg AS (
    SELECT
        trade_date,
        stddev_samp(intraday_ret) AS xs_disp,
        avg(intraday_ret)         AS xs_mean,
        count(*)                  AS n_names
    FROM sess_ret
    GROUP BY trade_date
    HAVING count(*) >= 30
),
seq AS (
    SELECT
        trade_date, xs_disp, xs_mean, n_names,
        row_number() OVER (ORDER BY trade_date) AS rn
    FROM day_agg
),
pit AS (
    SELECT
        s.rn,
        s.trade_date,
        LAG(s.xs_disp) OVER (ORDER BY s.trade_date) AS prior_disp,
        LAG(s.xs_mean) OVER (ORDER BY s.trade_date) AS prior_mkt_ret,
        avg(s.xs_mean) OVER (ORDER BY s.trade_date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS trail5_mean
    FROM seq s
),
-- PIT trailing percentile of prior_disp: for each day D, rank prior_disp among the prior_disp
-- values of the trailing 120 sessions strictly before D (i.e. window rn in [D.rn-120, D.rn-1]).
ranked AS (
    SELECT
        d.rn,
        d.trade_date,
        d.prior_disp,
        d.prior_mkt_ret,
        d.trail5_mean,
        count(w.prior_disp)                                   AS n_trailing,
        count(*) FILTER (WHERE w.prior_disp <= d.prior_disp)  AS n_le
    FROM pit d
    LEFT JOIN pit w
      ON w.rn BETWEEN d.rn - 120 AND d.rn - 1
     AND w.prior_disp IS NOT NULL
    WHERE d.prior_disp IS NOT NULL
    GROUP BY d.rn, d.trade_date, d.prior_disp, d.prior_mkt_ret, d.trail5_mean
)
SELECT
    trade_date,
    prior_disp,
    prior_mkt_ret,
    trail5_mean,
    CASE WHEN trail5_mean > 0 THEN 1 WHEN trail5_mean < 0 THEN -1 ELSE 0 END AS trend_5d,
    CASE WHEN n_trailing >= 20 THEN n_le::float8 / n_trailing ELSE NULL END   AS disp_pctile,
    CASE WHEN n_trailing >= 20
         THEN LEAST(5, GREATEST(1, ceil((n_le::float8 / n_trailing) * 5)::int))
         ELSE NULL END                                                        AS disp_tier,
    rn AS session_index
FROM ranked
ORDER BY trade_date;
