-- Family A — ex-div overnight-label artifact diagnostic (Modeller, 2026-06-12)
-- For qa-2 independent verification. READ-ONLY. Reproduces:
--   (1) the -51.6bps mechanical drop on ex-div nights (forward open == ex-morning),
--   (2) that the drop ~= dividend yield, and the yield-back restoration to baseline.
-- v1.1.1 labels are FROZEN (no version column) — this only READS them; never writes.

-- (1) Overnight label by ex-div alignment. The overnight label is close(D)->open(D+1) excess
-- vs the universe median, anchored at D's 15:30 ET. The mechanical ex-div drop appears on the
-- label whose FORWARD open is the ex-morning, i.e. label_date + 1 == ex_date.
WITH div AS (
  SELECT symbol, ex_date, cash_amount
  FROM corporate_actions_pit
  WHERE action_type = 'cash_dividend'
    AND ex_date BETWEEN '2024-01-02' AND '2026-06-11'
),
lbl AS (
  SELECT symbol, (ts AT TIME ZONE 'America/New_York')::date AS label_date, value
  FROM labels WHERE horizon = 'overnight'
)
SELECT
  CASE
    WHEN d_same.symbol IS NOT NULL THEN 'ex_date == label_date (fwd open is post-ex; expect ~baseline)'
    WHEN d_next.symbol IS NOT NULL THEN 'ex_date == label_date+1 (fwd open is ex-morning; expect DROP)'
    ELSE 'non-ex baseline'
  END AS bucket,
  count(*) AS n,
  round(avg(l.value)::numeric, 6) AS mean_overnight_label,
  round(stddev(l.value)::numeric, 6) AS std
FROM lbl l
LEFT JOIN div d_same ON d_same.symbol = l.symbol AND d_same.ex_date = l.label_date
LEFT JOIN div d_next ON d_next.symbol = l.symbol AND d_next.ex_date = l.label_date + 1
GROUP BY 1 ORDER BY 2 DESC;

-- (2) Is the drop ~= the dividend yield? Turn cash_amount into a yield via the prior RTH close
-- (15:59 ET backfill bar) and show the hygiene-corrected label = label + yield restores baseline.
WITH div AS (
  SELECT symbol, ex_date, cash_amount FROM corporate_actions_pit
  WHERE action_type = 'cash_dividend' AND ex_date BETWEEN '2024-01-02' AND '2026-06-11'
),
lbl AS (
  SELECT symbol, (ts AT TIME ZONE 'America/New_York')::date AS label_date, value
  FROM labels WHERE horizon = 'overnight'
),
px AS (
  SELECT symbol, (ts AT TIME ZONE 'America/New_York')::date AS d, close
  FROM bars_1m
  WHERE source = 'backfill' AND (ts AT TIME ZONE 'America/New_York')::time = '15:59'
)
SELECT
  count(*) AS n_ex_nights,
  round(avg(l.value)::numeric, 6)                                            AS mean_label,
  round(avg(-d.cash_amount / NULLIF(px.close, 0))::numeric, 6)               AS mean_neg_div_yield,
  round(avg(l.value + d.cash_amount / NULLIF(px.close, 0))::numeric, 6)      AS mean_label_plus_yield_hygiene
FROM lbl l
JOIN div d ON d.symbol = l.symbol AND d.ex_date = l.label_date + 1
LEFT JOIN px ON px.symbol = l.symbol AND px.d = l.label_date;
