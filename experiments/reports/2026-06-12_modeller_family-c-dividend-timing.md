# Family-C: dividend-timing features (the ex-date run-up anomaly)

**Agent:** modeller · **Date:** 2026-06-12 · **Status:** COMPLETE — NO EDGE (killed).

## 1. Hypothesis (pre-registered)
The dividend run-up / post-ex drift is a firm-calendar effect orthogonal to the exhausted price panel.
PRE-REGISTERED ~65% NULL prior (effect is small, slow; our horizon is short). First genuinely-new deep
data family tested end-to-end (live corporate_actions feed, 7133 cash dividends, 612 symbols, 2023-2026).

## 2. Exploration
DATA: corporate_actions joined to v1.1.1 panel (4.84M rows 30m / 428K overnight; 489/612 payers in
panel). FEATURES (strictly PIT, lookahead-guarded — upcoming ex-date counted only if within 35 cal days):
days_to_ex, days_since_ex, in_runup_window(≤5d), is_dividend_payer. GATES: full battery (IC + canary +
net-of-cost + survivorship). Script: experiments/family_c_dividend_timing.py.

## 3. Results
| horizon | baseline IC | +family_c IC | family_c-ONLY IC | family_c canary |
|---|---|---|---|---|
| fwd_30m | 0.02698 (be 1.42) | 0.02741 (be 1.51) | −0.0002 | 0.0026 |
| overnight | 0.01420 (be 3.20) | 0.01931 (be 4.72) | 0.0214 | **0.0145** |
Overnight family_c-only neutralized sharpe: −1.17. 30m: adding family_c moves IC +0.0004 = noise.

## 4. Verdict
**NO EDGE.** 30m: dividend cycle dead (family_c-only IC ~0, canary > |IC|). Overnight: the IC LOOKS
high (0.0214) but it's a TRAP — the canary is 0.0145, so ~68% of the apparent IC reproduces on SHUFFLED
labels (artifact, not alpha), and survivorship-neutral sharpe is NEGATIVE (−1.17). The canary caught it
again. Matches the pre-registered ~65% null. The firm-dividend calendar is exhausted as an edge source
(this + the ex-div overnight honest-negative + the later post-ex-drift shape null = three dividend nulls).

## 5. Next steps + declined
- FEEDS the strategic read: even orthogonal firm-calendar data adds 0 at our horizon → "data-starved,
  not model-starved; the remaining live new-data hope is MICROSTRUCTURE (OFI), not slow firm events."
- DECLINED: yield-magnitude or sector-conditioned dividend cuts (the base effect is absent; conditioning
  a zero won't create signal). Dividend family CLOSED.
