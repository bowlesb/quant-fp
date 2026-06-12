# Proposal 003 — Post-ex-dividend drift/reversal (SHAPE 3, event-reaction)

**Author:** explorer-shapes · **Date:** 2026-06-12 · **Status:** SUBMITTED (awaiting Lead disposition)
**Cost-structure rank: #3.** SPARSE by construction (event-triggered) -> structurally low-turnover. LIVE data, no bar re-scan.

## Hypothesis (mechanism story)
Names that have just gone ex-dividend exhibit a predictable post-event price path in the following
1-5 trading days: **dividend-capture unwind** (holders who bought only to capture the dividend sell
after ex-date, pressing the price down → a reversal-up opportunity), OR **post-drop drift** (the
mechanical ex-date drop over-shoots/under-shoots and continues). Direction is an empirical question,
conditional on yield size. This is a genuinely different shape AXIS — event-reaction, not continuous
cross-sectional ranking.

## What it is NOT (important — distinct from existing ex-div work)
The team's ex-div work so far is label HYGIENE: REMOVING the mechanical ex-date drop from the
overnight label so the model can't cheat by predicting it. THIS proposal does the opposite — it
TRADES the post-event drift/reversal as a signal. Different goal, different label, no overlap.

## Why it is cost-advantaged
The signal FIRES ONLY on ex-dates: ~7,133 cash-dividend events across 2020-2026 in
`corporate_actions` (633 symbols). Across a 600-day panel that's a tiny fraction of name-days →
participation is inherently sparse → turnover is structurally tiny. Event-triggered, not continuous.
Restrict to the liquid head and it's the cheap tier too.

## Label (NEW — coordinate via the Lead)
`post_exdiv_drift`: for each (symbol, ex_date) in `corporate_actions WHERE action_type='cash_dividends'`,
the forward N-trading-day return from the ex-date close, N ∈ {1,3,5}. Cross-sectionally demeaned
WITHIN the ex-date cohort (or vs the universe on the same date) to strip market beta. Yield =
cash_rate / prior_close is the primary conditioning feature. PIT: the ex_date is known in advance
(corporate_actions is announced ahead), so trading from the ex-date open/close is point-in-time legal.

## Pre-registered result that would FALSIFY
If the forward N-day return shows no relationship to dividend yield AND no consistent sign across the
event cohort (mean post-ex return statistically indistinguishable from the universe, |t| < 2 at all
N), the post-ex-dividend drift shape is dead. Pre-registered prior: ~30% — dividend-capture effects
are documented historically but heavily arbitraged in liquid US names post-2010; more plausible in
the smaller/higher-yield tail, which is also the EXPENSIVE-to-trade tail (tension to report honestly).

## Gates (all present)
- **Shuffle canary:** shuffle the ex-date→symbol assignment, confirm the effect vanishes.
- **Survivorship neutralization:** per-symbol demean (some high-yielders are persistent payers).
- **Net-of-cost:** per-name half-spread; the high-yield tail is often the WIDE-spread tail — gate on
  the liquid subset and report how many events survive the cost filter (likely few — report honestly).
- **Multiple-testing:** 3 horizons (N=1,3,5) — flag to the Lead.
- **Sample-size honesty:** report the number of *liquid-tier* events; a dividend-capture edge that
  only exists in untradeable micro-yield names is not a finding.

## Cheapness
★★ — uses LIVE corporate_actions (no bar re-scan); one new event-anchored label compute. Runnable now
(does NOT depend on proposal 000). Anchored on ex_date close, which is in the daily helper or a direct
small bars query keyed by the (sparse) event dates.

## Lead disposition
<!-- Lead fills -->

## LEAD DISPOSITION — APPROVED (priority 2 of shapes lens, runnable NOW), 2026-06-12
Validated: gates present; correctly DISTINCT from both Family C (dividend-timing FEATURES, which I just
verdicted NO-edge) and the ex-div label-hygiene work — this TRADES the post-event drift as an event-
reaction shape with a new ex-date-anchored label. Runnable now (LIVE corporate_actions, no bar re-scan;
ex-date close anchor). HONEST PRIOR you set (~30%, arbitraged in liquid US names; the effect lives in the
high-yield = wide-spread = expensive tail) is the right tension — and Family C's overnight dividend null
LOWERS it further, so I'm flagging: this needs to beat not just the canary but the Family-C precedent.
Report the count of LIQUID-tier events (a capture edge in untradeable micro-yield names is not a finding).
3 horizons (N=1,3,5) noted against the global count. BUILD the event-anchored label + battery. ENQUEUE on
delivery. Good cheap event-reaction shape — exactly the "beyond cross-sectional L/S" axis Ben wants.
