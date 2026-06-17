# R1 Stage 2a — give-back prediction RESULTS (bars sequence)

Run: `stage2_giveback.py` on the 637/643 CORE runner-days with f30 bars. Walk-forward split at
2026-01-16 (train 444 / test 193). lightGBM. Pre-registered in stage2_giveback_prereg.md.

## Results (walk-forward OOS)
- **Give-back MAGNITUDE: NOT forecastable.** OOS R2 = **−0.0074** (≈ 0). The first-30-min bar path
  does NOT predict how far the runner gives back. (The pre-reg's "skill-over-canary +0.39" was a
  BAD test — it compared to a label-shuffled model's pathologically-negative R2 of −0.40; beating a
  terrible baseline is not skill. The honest baseline is R2 = 0, and we are AT zero. Verdict logic
  corrected in the script.)
- **Hard-fade DIRECTION: mild signal.** OOS AUC = **0.707** for P(give-back ≤ −10%), base rate 0.65.
  There is modest classification signal for WHETHER a runner fades hard — but the base rate is
  already 65%, so the lift is small.
- Top importances: intra_pullback, ret_vol, runup_slope, early_move, gap_open. These just re-encode
  "already pulling back off the high by 10:00 + a big volatile run = fades more" — which is exactly
  what F9's `runner_pullback_from_high` and `runner_early_move` already expose.

## Verdict
- **STRATEGY:** the give-back magnitude is essentially RANDOM given the path — you cannot size the
  short by predicted depth. The mild hard-fade AUC (0.707) says the fade is slightly more likely when
  the name is ALREADY fading by 10:00 + ran up violently — a weak conditioner, not a sizing signal.
  The unconditional fade (Stage-1: median −17.8% intraday, −6 to −14% multi-day, 65–70% fade) IS the
  edge; the path adds little. The (gated) short thesis stands on the UNCONDITIONAL fade, not on
  forecasting which runner fades hardest.
- **FEATURE (batch-1c candidate): NO new feature warranted.** A `predicted_giveback` feature would
  (a) lean on a near-zero-R2 model and (b) be REDUNDANT — its entire predictive content is
  intra_pullback + early_move, both already in F9. Per the mandate a feature must be NON-redundant;
  this fails that bar. F9's `runner_pullback_from_high` + `runner_is_active` already give the model
  everything Stage-2a found. **Do not add a redundant feature. F9 stands as the runner feature.**

## What this rules in / out
- RULED OUT: a bar-path give-back-magnitude predictor (and therefore a derived feature). Honest null.
- STILL OPEN (Stage 2b, gated, exploratory): tick MICROSTRUCTURE on the 137 tick-covered runner-days
  may carry give-back-timing signal the 1-min bars smear out (e.g. the trade-size distribution / the
  print that marks the top). But n=137 with walk-forward is underpowered — this is a LOW-priority,
  clearly-exploratory probe, NOT a powered result, and it does NOT gate anything. Given Stage-2a's
  clean null on bars, the prior that ticks rescue magnitude-prediction at n=137 is low; I am NOT
  prioritizing it over fresh feature lanes.

## Net for the program
R1 delivered its compounding output already: F9 runner_state (SHIPPED, merged in #73). Stage 2a is
an honest null that PREVENTS a redundant feature and correctly scopes the runner short to its
unconditional-fade thesis. The GPU is freed for a job where n justifies it (the 137-tick model does
NOT) — next GPU candidate is the productionized embeddings (now merged, #76) as a parity-true group,
pending the embeddings agent / Lead.
