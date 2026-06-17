# R1 — Small-cap MORNING RUNNERS: predict continuation vs reversal (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Ben's high-priority direction. The thesis (and why it's NOT a
repeat of the cycle-1/2 illiquid kills): morning runners — $2-$20 semi-liquid names making aggressive early
moves (+50% to +200%) — have HUGE signal magnitude that DWARFS the spread/impact. Catching even PART of a
50%+ move is hugely net-positive even at a 50-200 bps small-cap spread. **The COST gate that killed the
bps-sized liquid factors does NOT bind for big-move events.** The binding constraint FLIPS to
EXECUTION-REALITY (halts, LULD limit-up bands, un-fillable gap prints) — that is where this dies if it dies.

The edge thesis (Ben's): we have NO latency advantage vs HFT, but an edge at decent-timing × SIGNAL BREADTH —
combining MANY WEAK signals (tick features + the broad 617-feature set) that retail + simple algos can't
process. Predict which runners CONTINUE vs REVERSE at 5-MINUTE (intraday) AND MULTI-DAY horizons.

## Hypotheses (two horizons)
- **H-intraday:** among morning runners, a combination of microstructure features (signed flow, trade-freq
  burst, quote dynamics) + the broad feature set predicts the NEXT-5-to-30-min continuation vs reversal,
  net of REALISTIC fills (incl. halts/LULD), with a per-trade bootstrap CI > 0.
- **H-multiday:** the runner-day features predict the 1-5 day forward return (continuation vs the classic
  pump-and-fade reversal), net of realistic small-cap cost.

## STAGE 1 (this work-unit) — EVENT-SET CHARACTERIZATION (pure bars, no tick backfill)
Before any prediction, characterize the runner universe from the 378-day bars (all ~7,600 symbols):
- **Runner event definition (pre-committed, tunable in stage 1):** a (symbol, date) where, at a price tier
  $2-$20 (prior close), the name shows a large EARLY-SESSION move: e.g. (open or first-30-min high) /
  prev_close − 1 ≥ +30% (sweep 30/50/100%), AND a volume surge (first-30-min volume ≥ K× the trailing-20-day
  median first-30-min volume), AND prev_close in [$2,$20]. Tune the thresholds to the "+50-200%" target Ben
  named; report the runner COUNT at each threshold.
- **Characterize:** how many runner-days over 378d? per how many unique symbols? distribution of the gap/early
  move size, the price tier, the volume surge, the intraday path (does it keep running or fade by close?),
  the next-day return (the pump-and-fade question). What FRACTION continue vs reverse intraday + multi-day
  (the base rates — the prediction target's class balance).
- **Survivorship caveat:** the bars are the CURRENT universe (delisted runners absent) — flag it; many
  pump-and-dumps delist, so the surviving set is survivorship-biased toward... TBD (note honestly).

## STAGE 2 (next work-unit, gated on stage 1) — the prediction
If stage 1 shows enough runners (target ≥ a few hundred events) with a non-trivial continue/reverse split:
- Coordinate with the backfill agent's SELECTIVE TICK BACKFILL to fetch ticks for the runner names/days
  (they're NOT in the liquid-1000 tick set — this is exactly what that tooling is for).
- Build tick + broad features at the decision point (e.g. the first-30-min mark); predict continuation/
  reversal at 5-30 min + 1-5 day. Breadth × ML (a natural GPU-lane fit).

## THE ACUTE TRAPS (pre-committed — this is where it dies if it dies)
1. **TRADEABLE ENTRY / EXECUTION REALITY (the binding gate here, replacing cost):** can you actually BUY at
   the signal price? Runners gap, HALT, hit LULD limit-up bands (no shares offered). Model realistic fills:
   NO buying an un-fillable gap print; enter at a tradeable bar ≥ the decision minute at a price that
   reflects the spread crossed + a halt/LULD-aware fill (if the name is limit-up-halted, you CAN'T buy). A
   signal that "works" only by buying the un-fillable print is the artifact. This is the #1 kill risk.
2. **SURVIVORSHIP:** delisted runners are absent from the current-universe bars — the surviving set is biased.
   Flag it; the honest version needs delisted names (a data ask) for stage 2's tradeable claim.
3. **PER-TRADE BOOTSTRAP on realistic fills** (not the IC): the headline is the realized per-runner-trade net
   P&L distribution, bootstrap CI > 0, at the modeled halt/LULD/spread fills.
4. **CLASS-IMBALANCE / base-rate:** if 80% of runners continue (or reverse), a naive "always predict the
   majority" looks good — the canary/skill metric must beat the base rate, not just be positive.

## Expected / confidence
- Confidence a tradeable continuation/reversal edge survives the EXECUTION-REALITY gate net of realistic
  fills: **~30%** — HIGHER than the cycle-1/2 liquid factors because the cost gate doesn't bind (big moves),
  and the breadth×ML angle is genuinely untested on this event class. The kill risk is execution (halts/LULD/
  un-fillable prints) + survivorship, NOT cost. Pre-commit the prior.
- STAGE-1 success = a characterized event set (counts, base rates, the continue/reverse split) sufficient to
  justify the tick backfill for stage 2. STAGE-2 KEEP = a per-trade-bootstrap-positive prediction net of
  modeled halt/LULD/spread fills, beating the base rate, OOS.

## DUAL OUTPUT (the feature)
The runner-detection flag (`is_morning_runner`, `early_move_pct`, `early_volume_surge`) + the
continuation-probability are FEATURES for the all-features model too — a runner is a real, rare,
high-information state. Stage 1's detector is itself a feature candidate (groups-only over bars: gap +
early-volume-surge at a price tier).

## Ordering
STAGE 1 NOW (pure bars, fast, no dependency) — characterize + report. Then gate stage 2 (tick backfill +
prediction) on stage 1 showing a worthwhile event set. Dedicated explorer(s) per Ben.
