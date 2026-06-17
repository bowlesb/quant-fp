# R2 small-cap morning DUMPERS — RESULTS (bars, the R1 mirror)

Run: `characterize.py` (parallel, 7,682 symbols × ~379 trading days). Output: `dumper_events.parquet`
(802 rows / 642 syms at drop≥0.30, surge≥2). Dumper-day = prev RTH close ∈ [$2,$20] AND early_drop
(1 − first-30-min LOW / prev_close) ≥ thresh AND first-30-min vol surge ≥ thresh.

## CORE cell (drop ≥0.50, surge ≥3): 161 days / 149 syms
- runner-day $vol 10/50/90: **$2.35M / $16.7M / $94.7M** — markedly LESS liquid than runners
  ($154M median). The illiquid-tier caveat (untradeable bottom tiers) applies MORE here.

### The asymmetry vs R1 runners — the key finding
| horizon | RUNNERS (R1) | DUMPERS (R2) |
|---|---|---|
| intraday (close vs f30 extreme) | FADE: −17.8% off the high | **BOUNCE: +8.7% off the low** (48% bounce >10%, only 25% close below the low) |
| multi-day fwd 1d | −6.3% (32% up) | **−6.4% (35% up)** |
| multi-day fwd 5d | −13.9% (30% up) | **−12.5% (33% up)** |

- **INTRADAY the dumper BOUNCES** (partial reversal of the morning panic): median close +8.7% above
  the f30 low; nearly half bounce >10% off the low. This IS the symmetric overshoot-reversal — the
  −50% morning crash overshoots and recovers intraday, mirroring the runner's intraday fade.
- **MULTI-DAY the dumper CONTINUES DOWN** (−6.4% 1d, −12.5% 5d; ~33% up) — NOT a symmetric bounce.
  The −50% drop on a $2-20 name is largely REAL distress / dilution / bad news, and it keeps bleeding.

So the regimes are ASYMMETRIC, and that asymmetry is the informative result:
- RUNNER: overshoot UP, then mean-revert DOWN at BOTH horizons (a pure overshoot both ways).
- DUMPER: overshoot DOWN, bounce intraday (panic overshoot), then continue DOWN multi-day (distress).
  Intraday = overshoot-reversal; multi-day = distress-continuation. The drop carries genuine
  bad-news information the run-up does not.

## Verdict
- **STRATEGY:** two distinct (gated) shapes — an intraday bounce-LONG (buy the −50% panic, ride the
  +8.7% median recovery) and a multi-day continuation-SHORT (the distress bleed). BOTH gated on
  EXECUTION REALITY, and HARDER than R1: dumpers are less liquid ($16.7M median), LULD-DOWN halts
  interrupt the panic-bottom entry, and the multi-day short needs borrow on distressed names. The
  intraday bounce-long is the more tradeable of the two (no borrow, but halt/limit-down risk). Not
  certified — a Stage-2 execution-reality job, lower priority than its FEATURE.
- **FEATURE (batch-1c candidate): SHIP — `dumper_state`** (the short-side mirror of F9, distinct by
  the asymmetry). early_drop / gap_open / bounce_from_low (running, point-in-time) + in_band +
  is_active. NON-redundant with F9: F9 tracks the run-UP off the high; this tracks the DROP and the
  bounce off the LOW — opposite tail, and the multi-day-down vs runner's both-ways-down asymmetry is
  a regime a model cannot derive from the runner features. Real + parity-true + non-redundant +
  not-noise → clears the feature bar. Spec + build below.
