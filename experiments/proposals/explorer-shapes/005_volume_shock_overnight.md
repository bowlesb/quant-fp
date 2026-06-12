# Proposal 005 — Volume-shock overnight reversal, bar-only (SHAPE 4, conditional overlay)

**Author:** explorer-shapes · **Date:** 2026-06-12 · **Status:** SUBMITTED (awaiting Lead disposition)
**Cost-structure rank: #5 (cheapest to TEST).** Reuses the EXISTING overnight label — only needs a volume_z gate. LOW-TURNOVER + SPARSE.

## Hypothesis (mechanism story)
A name with a volume shock today (today's volume >> its trailing average) has likely seen a
liquidity-driven price move that partially reverses overnight/next session — forced/impatient flow
over-extends intraday, then mean-reverts once the pressure clears. The overnight gap is where some of
that reversal realizes. Conditioning on the shock makes this a SPARSE overlay, not a continuous book.

## Why it is cost-advantaged AND cheap to test
- Uses the **EXISTING overnight label** — no new label, no new compute on the label side.
- Only needs `volume_z` (today volume / trailing ADV) from existing bars — a cheap feature.
- LOW-TURNOVER (overnight hold) and SPARSE (fires only on shock days, a minority of name-days).
This makes it the single cheapest shape to actually run: an overnight battery is already wired; this
just adds a participation gate on volume_z.

## Honest caveat (why it's ranked last)
The overnight label is SURVIVORSHIP-NEGATIVE across EVERYTHING tested so far (raw/rank/vol/lambdarank,
pre- and post-ex-div-correction). So the prior is genuinely low. BUT every prior overnight test traded
the WHOLE book; this asks a narrower, untested question: **does conditioning on a volume shock — a
sparse subset — rescue an otherwise-dead label?** It's a cheap, fair, pre-registered re-test of the
sparsity hypothesis on the label we already have, not a re-run of a dead config.

## Label
EXISTING `overnight` cross-sectional excess. No new label.

## Method
For shock thresholds volume_z ∈ {>2σ, >3σ}: trade the overnight book ONLY on name-days where today's
volume_z exceeds the threshold; test BOTH directions (reversal: short up-movers / long down-movers
within the shock cohort; and continuation as the null alternative). Measure gross IC on the shock
cohort, participation rate, and net-of-cost survivorship-neutralized Sharpe.

## Pre-registered result that would FALSIFY
If the shock-cohort overnight IC is no different from the full-book overnight IC (sparsity buys
nothing) AND the survivorship-neutralized net Sharpe stays ≤ 0 (as it does full-book) — the
volume-shock overlay is dead and we close the overnight label as a shape entirely. Pre-registered
prior: ~20% (low — the overnight label is dead full-book; sparsity is the only untested lever).

## Gates (all present)
- **Shuffle canary** on the shock-cohort overnight label.
- **Survivorship neutralization** (per-symbol demean) — MANDATORY here; the full-book version was
  survivorship-driven, so this gate decides the verdict.
- **Net-of-cost** with per-name half-spread + fill-asymmetry (overnight L/S has the same short-underfill
  exposure — consider the long-only down-mover-reversal variant).
- **Turnover honesty:** report participation (shock-day fraction) and overnight turnover.
- **Multiple-testing:** 2 thresholds × 2 directions — flag to the Lead.

## Cheapness
★★ (bar-only). Richer OFI/trade-intensity version is M2-gated (trade_agg only 52 symbols now) — log
as a follow-up once order-flow scales to >=500 names.

## Lead disposition
<!-- Lead fills -->

## LEAD DISPOSITION — APPROVED (priority 3 of shapes lens, cheapest, runnable NOW), 2026-06-12
Validated: gates present; uses the EXISTING overnight label + a cheap volume_z gate — cheapest shape to
run. HONEST PRIOR is correctly LOW (~20%): the overnight label is survivorship-NEGATIVE across everything
tried (raw/rank/vol/lambdarank, pre/post ex-div). Your framing is fair though — sparsity (shock-cohort)
is the ONE untested lever on that label, and survivorship-demean is the make-or-break gate (MANDATORY,
you flagged it). Run it as a cheap fair re-test, not a re-run of a dead config. 2 thresholds x 2
directions noted against the global count. If it dies, we CLOSE the overnight label as a shape entirely —
a valuable closure. BUILD on the existing overnight battery + volume_z gate. ENQUEUE/run on delivery.
