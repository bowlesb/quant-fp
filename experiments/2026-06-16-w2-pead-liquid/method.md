# W2 — Method: item-2.02 PEAD on LIQUID names

Pre-registered in `hypothesis.md` (2026-06-16, BEFORE running). This file documents the exact
mechanics; `results.md` has the real tables; `verdict.md` the KEEP/AMBIGUOUS/KILL decision.

## 1. Earnings-event identification (item-code parse)
- Pull all 8-Ks from the `filings` table with `available_at >= 2025-12-15` (genuine UTC; PIT/look-ahead-safe).
- For each distinct `cik`, call the SEC submissions API
  `https://data.sec.gov/submissions/CIK{cik}.json` (User-Agent
  `quant-fp research ben.bowles@gmail.com`, ~0.12 s sleep for the 10/s limit). The
  `filings.recent` block has parallel arrays `accessionNumber[]` and `items[]`.
- The DB `accession_number` is already in the SEC dashed form (`0000769397-26-000041`), so we match
  each DB 8-K to its SEC `items` string by **exact accession_number**. No format normalization needed.
- **EARNINGS event = an 8-K whose item list contains `2.02`** (Results of Operations and Financial
  Condition). This is the canonical earnings-release item.
- CIKs whose submissions are too old to appear in the SEC `recent` block (last 1000 filings) are
  simply unmatched; we report the matched fraction (`n_matched / n_8k`). Within our window
  (since 2025-12-15) essentially all 8-Ks fall inside `recent`.

## 2. Universe + liquidity tier (PRIMARY gate)
- Daily panel built from `/store/raw/bars` minute bars (read-only mount). Per (symbol, date):
  `close` = last RTH bar, `open_price` = first RTH bar at/after 13:30 UTC (= 09:30 ET, summer),
  `dollar_vol` = sum(close*volume) over RTH bars. RTH = UTC hour in [13,21], open eligibility
  `(hour==13 & minute>=30) | hour>13`.
- `adv_dollar` = median daily dollar-volume per symbol over the last 20 trading days.
- **LIQUID tier = top tertile by `adv_dollar`** (the H10b lesson: the prior pooled-8-K drift lived in
  the illiquid tail and died in liquid names — so the liquid tier is the PRIMARY report). A top-100
  megacap cut is also reported as a robustness slice. `mid`/`illiquid` tertiles are full-universe context.

## 3. Entry, forward returns, event sign
- **Entry = D+1 OPEN** after `available_at` (the next trading day's open; tradeable, never the filing
  instant). Forward returns computed open-entry → close at horizons {1, 3, 5, 10, 20, 40} trading days:
  `open_fwd_Hd = close[t+H] / open_price[entry] - 1`.
- **Event sign (reaction proxy — no estimates feed):** PEAD = drift continues in the direction of the
  earnings surprise. Lacking a consensus-estimate/SUE feed, we proxy the surprise by the **immediate
  reaction = the D+1 open→close abnormal return** (event return minus same-date control mean), i.e.
  `sign = sign(reaction)`. Positive-reaction earnings → LONG; negative-reaction → SHORT. The forward
  drift is then measured in the *signed* direction. (CAVEAT, pre-flagged: the reaction-sign proxy is
  noisier than a true SUE and can conflate the reaction itself with the drift; a clean SUE needs an
  estimates feed = a data ask. To avoid the reaction return contaminating the H=1 drift, the sign is
  taken from the D+1 reaction and the *drift* is measured from the **D+1 close onward** for the signed
  L/S leg — see §4.)

## 4. Portfolio construction
- **Headline (un-signed) PEAD drift:** equal-weight the LIQUID earnings cohort's open-entry forward
  return at each horizon, minus the same-date non-event control mean (per-symbol-demeaned), day-clustered.
  This is the "does earnings drift exist at all in liquid names" number.
- **Signed L/S (the tradeable bet):** sign each event by its D+1 reaction; LONG positive, SHORT
  negative; the per-trade signed drift = `sign * (close[t+H]/close[entry] - 1)` measured from the **D+1
  close** (the bar after the reaction is known) to avoid using the reaction return as both signal and
  payoff. Equal-weight across the cohort; control = same-date non-event names.
- **Net of cost:** deduct the measured LIQUID round-trip cost (median liquid half-spread ×2 for a
  round trip) and a 2× stress. Liquid half-spreads measured from `/store/raw/quotes` exactly as H13.

## 5. Gates
- **Shuffle-canary:** within each event date, permute the event/control labels (10 seeds); the canary
  alpha must sit at ~0 with the real alpha well outside its p95.
- **Per-symbol demean:** subtract each symbol's own mean forward return (null-safe group-mean join)
  before forming the cohort-minus-control alpha — kills any static per-name level effect.
- **Walk-forward OOS:** split the trading days in half by date and report TRAIN vs OOS (this is an
  event study with no fitted parameters). NOTE (discovered at run time): the bar panel spans
  2024-12-11..2026-06-16 (378 days) but the `filings` table only holds 8-Ks from 2025-12-15 onward,
  so ALL earnings events fall in the OOS half (TRAIN has 0 events). The decisive number is thus the
  full-event-set leg, defended by demean + shuffle-canary + per-trade bootstrap rather than an
  independent in-sample fit; a longer EDGAR backfill is the data ask for a true walk-forward.
- **Per-trade bootstrap:** 10k resamples of the realized D+1→D+H signed round-trip trades (the actual
  tradeable units), net of cost; KEEP requires the bootstrap CI to **exclude zero above** (lower bound
  > 0) on the LIQUID OOS leg.
- **Cost gate:** measured liquid spread, and spread×2 stress; net drift must survive.

## 6. Decisiveness
DECISIVE = LIQUID OOS signed L/S **net-of-cost** drift with per-trade bootstrap CI lower bound > 0,
demean-surviving, canary-clean. KILL if the liquid tier is dead (illiquid trap again) or net ≤ 0.

VECTORIZED throughout (polars panel ops + numpy bootstrap). Metric helpers (`spearman_ic`,
`day_clustered_tstat`, null-safe demean) imported from the sibling `hf_metrics_fixed.py`.
