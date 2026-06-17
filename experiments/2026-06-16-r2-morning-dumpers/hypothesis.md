# R2 — small-cap morning DUMPERS (the R1 mirror), PRE-REGISTERED

The symmetric counterpart to R1 runners. R1 found small-cap morning RUNNERS ($2-20, +50%+ in the
first 30 min on a vol surge) FADE. Does the mirror hold for DUMPERS?

## Definition
Dumper-day = prev RTH close ∈ [$2,$20] AND early_drop (1 − first-30-min LOW / prev_close) ≥ 0.30
AND first-30-min vol surge (f30 vol / trailing-20d median f30 vol) ≥ 2. CORE = drop ≥ 0.50, surge ≥ 3.

## Question (pre-registered, BEFORE seeing results)
Two symmetric hypotheses:
- BOUNCE (mirror of the runner fade): a violent −50% morning drop on a small-cap OVERSHOOTS and
  bounces back by EOD / over the next days (mean-reversion, the symmetric reversal). H_bounce.
- CONTINUATION (distress): a −50% small-cap drop is a real solvency/dilution shock and continues
  DOWN (the reverse-split / distress pattern H4 found — drops on $2-20 names are often genuine
  bad news, not overshoot). H_down.

These make OPPOSITE predictions, so the test is decisive either way.

## Measurements (bars, all 379 days)
- counts across drop×surge cells; CORE distribution (prev_close, $vol/capacity)
- INTRADAY: close vs f30-LOW (median; frac bouncing >10% off the low; frac closing BELOW the low)
- MULTI-DAY: fwd 1d / 5d median + frac up
- capacity (runner-day $vol) — is the dumper liquid enough to trade?

## Falsification / read
- If close-vs-low is strongly POSITIVE (bounce) AND fwd returns positive → H_bounce (a symmetric
  reversal regime; runners fade, dumpers bounce — a clean overshoot story).
- If close-vs-low ≈ 0 or negative AND fwd negative → H_down (distress continuation; the asymmetry is
  the interesting finding — runners overshoot UP but dumpers are real bad news).

## Dual output
- Strategy: a (gated) bounce-long or continuation-short, same execution-reality gate as R1 (halts,
  LULD-DOWN bands, borrow if short).
- FEATURE (batch-1c candidate): a `dumper_state` regime detector (early_drop, in_band, is_active,
  bounce-from-low) — the SHORT-side mirror of F9 runner_state. A model gains the small-cap-crash
  regime conditioning variable. Ships ONLY if real + parity-true + NON-redundant with F9 (the drop
  side is genuinely distinct from the run-up side) + not noise.
