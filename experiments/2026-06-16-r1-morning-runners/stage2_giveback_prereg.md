# R1 Stage 2a — runner give-back prediction (PRE-REGISTERED, bars sequence)

Pre-registered BEFORE running. Predict the runner's give-back from its first-30-min PATH, to test
whether the fade is forecastable (which runners fade hard vs the ~30% that continue) — and to mine
a give-back feature candidate for batch-1c.

## Sample
The 643 CORE runner-days (early_move>=0.50, surge>=3, prev_close $2-20) from Stage 1. BARS for all
643 (ticks exist for only 137 → tick refinement is Stage 2b, exploratory, NOT this powered cut).

## Features (first-30-min bar path, point-in-time as of 10:00 ET — NO look-ahead past the f30 window)
Per runner-day, from the 09:30-10:00 ET minute bars + prior close:
- gap_open, early_move (f30 high/prev_close-1), prev_close (log), f30 dollar-vol (log)
- path shape: minute of the f30 high (when did it peak), run-up slope, # green vs red minutes,
  max 1-min return, realized vol of 1-min returns, close-of-f30 vs f30-high (intra-window pullback)
- vol surge, ratio of last-5min vol to first-5min vol (is volume accelerating or fading by 10:00)
All strictly within 09:30-10:00; the LABEL is measured AFTER.

## Labels (two, both from a TRADEABLE reference, not the peak tick)
- giveback_eod = rth_close / f30_high - 1 (the EOD give-back; Stage-1 median -17.8%)
- fwd5d = close[t+5] / rth_close - 1 (multi-day continuation; Stage-1 median -13.9%)
Regression (magnitude) + a binary "hard fade" (giveback_eod <= -0.10, the 65% base rate).

## Protocol (anti-mirage)
- WALK-FORWARD by DATE: train on the earliest 70% of dates, test on the latest 30% (strict time
  split — no future leakage). Report OOS only.
- SHUFFLE CANARY: refit on label-permuted train, score same OOS — OOS skill must exceed the canary.
- Clustering unit = the runner-DAY (each event is one independent observation; no overlapping
  windows since each is a distinct symbol-day).
- Model: lightGBM (gradient-boosted; right tool at n=643, NOT a deep net — that's the 137-tick job).
- Metrics: OOS R2 (regression), OOS AUC + base-rate-relative lift (hard-fade), feature importance.

## Falsification
If OOS R2 <= the shuffle canary (no skill beyond the unconditional fade), the give-back is NOT
path-forecastable from bars → the unconditional fade is the whole signal, and the feature candidate
is just runner_is_active (already in F9). Honest negative.

## Dual output
- Strategy refinement: if forecastable, the short sizes UP on predicted-hard-fade runners.
- FEATURE candidate (batch-1c): predicted_giveback as a point-in-time score at 10:00 ET — but ONLY
  if it beats the canary AND can be made parity-true (a deterministic function of f30 bars). If the
  signal is just "bigger early_move -> bigger fade," that's already in F9 and we DON'T add a redundant feature.
