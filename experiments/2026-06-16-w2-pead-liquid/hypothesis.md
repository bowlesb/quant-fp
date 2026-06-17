# W2 — PEAD (post-earnings-announcement drift) on LIQUID names, item-2.02-confirmed (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Lens L1/L5. Friction-wall design: a LARGER INFORMATION SHOCK
(earnings) re-priced over DAYS (low turnover), isolated to LIQUID names (where cycle-1's pooled 8-K drift
DIED — it was illiquid-concentrated). PEAD is one of the most-replicated anomalies; the question is whether
the LIQUID-tier, earnings-specific drift clears cost net (it's more arbitraged in large-caps, but also a
larger, slower move than the microstructure signals that failed).

## What cycle-1 established (and why this is different)
Cycle-1 H10 pooled ALL 8-K filings and found a real drift that was ILLIQUID-CONCENTRATED (liquid tertile
dead, H10b). BUT it pooled earnings 8-Ks (item 2.02) with non-earnings ones, on a 7% item sample, and never
cleanly isolated the LIQUID earnings subset. W2 isolates exactly that: item-2.02 (earnings) 8-Ks, LIQUID
tier only, the canonical PEAD setup.

## Universe + data
- `filings` table (3.2M PIT, available_at look-ahead-safe). Parse item codes from the SEC submissions
  `items` field (one call per CIK — verified available: e.g. "2.02,9.01"). Earnings event = an 8-K with item
  2.02 present.
- LIQUID universe = top tertile (and a top-100 megacap cut) by median daily dollar-volume from bars.
- Entry = D+1 OPEN after available_at (tradeable, never the filing instant; UTC-correct). Forward returns
  {1, 3, 5, 10, 20, 40} trading days (PEAD is documented to drift up to a quarter — go longer than H10's 10d).

## Test design
- The DRIFT direction: PEAD = price continues in the direction of the earnings surprise. Without a clean
  consensus-estimate feed we proxy the "surprise" by the EVENT-DAY (available_at-day or D+1) abnormal return
  (the market's immediate reaction) — drift continues in that direction. So: sign the cohort by the
  event-day reaction, then measure forward drift in that signed direction. (Flag: a true SUE/surprise needs
  an estimates feed = a data ask; the reaction-sign proxy is the standard alternative.)
- Cross-sectional, LIQUID-tier: long the positive-reaction earnings names, short the negative-reaction ones,
  held D+1→D+H; vs same-date non-event controls; equal-weight portfolio.
- GATES: shuffle-canary, per-symbol demean, walk-forward OOS, per-trade bootstrap on the realized D+1→D+H
  round-trips, cost gate at the measured LIQUID spread + 2×. The DECISIVE number: LIQUID-tier OOS portfolio
  net-of-cost drift, per-trade bootstrap CI > 0.
- LIQUID-tertile gate is PRIMARY (the H10b lesson): report it first; full-universe is context.

## Expected / confidence
- Confidence the LIQUID item-2.02 PEAD clears net-of-cost OOS with bootstrap CI > 0: **~35%** — higher than
  the price hypotheses because PEAD is the single most-robust documented drift, it's a large multi-day move
  (low turnover, friction-favorable), and isolating earnings + liquid is exactly the cut cycle-1 missed. The
  risk: PEAD is heavily arbitraged in LARGE-caps specifically (it survives mostly in small-caps — the very
  trap), and the reaction-sign proxy is noisier than a true SUE. Pre-commit the prior.
- KEEP-AS-LEAD: LIQUID OOS net positive, bootstrap CI > 0, demean+canary survived → an event-driven paper
  container + flag the estimates-feed data ask for a true SUE. AMBIGUOUS: liquid-marginal. KILL: liquid tier
  dead (the illiquid trap again) OR net ≤ 0.

## Friction-wall scorecard
[info-shock ✓ earnings] [low-turnover ✓ multi-day] [liquid-gated ✓ PRIMARY] — the cleanest event bet, and a
direct test of whether ANY event drift lives in tradeable names (cycle-1 found none did, but never isolated
liquid-earnings).
