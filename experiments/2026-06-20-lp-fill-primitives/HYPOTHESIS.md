# LP FILL PRIMITIVES — PRE-REGISTRATION (shared substrate, NOT an edge test)

**Date:** 2026-06-20 · Modeller (non-overlapping lane) · Branch `modeller/lp-fill-primitives` off
`origin/main` @ 79293b8. Data state: `/store/raw/quotes` 379d (2024-12-12 → 2026-06-18), deep core =
13 names with the full window (AAPL AMD AMZN AVGO GOOGL META MSFT NVDA ORCL PLTR QQQ SPY TSLA). Live fp
0x873f2fceb8f00c92 / 728 (unchanged — this is research infra, no group registered).

## What this is (and is NOT)

This is **shared research infrastructure**, pre-registered BEFORE any LP portfolio return is computed.
It is the honest microstructure cost/fill substrate the liquidity-provision (LP) surface needs and that
the #205 quote-spread re-test did NOT build (`retest.py` extracts only the liquidity-TAKING effective
half-spread). The active quote-thread (which just SETTLED the #205 spread re-test = 8th null and is
starting the LP surface) consumes these primitives instead of re-deriving fragile fill math inline.

It is **NOT** an edge test. It computes per-(symbol, day) microstructure primitives only. It forms no
portfolio, computes no forward cross-sectional return, and makes no alpha claim. The LP edge test —
posting passive orders against a directional/quote signal and measuring net P&L — remains the active
thread's job. This module exists so that test rests on ONE audited, pre-committed fill model rather than
an unfalsifiable inline assumption.

## The three primitives (pre-committed definitions)

1. **`quoted_half_spread`** = median RTH `(ask-bid)/2/mid` (return units). The spread a passive resting
   order EARNS at the touch. (Same definition as the #205 re-test's effective half-spread, for
   continuity.)

2. **`top_of_book_depth`** = median RTH `bid_size` / `ask_size`, in **ROUND LOTS (×100 shares)**, plus
   `touch_notional_usd`. The capacity / fill-rationing primitive. *Convention pinned:* sizes are lots,
   verified by notional sanity (AAPL median size 2 lots × 100 × ~$196 ≈ $39k, realistic; the raw-share
   reading gives ~$393, absurd).

3. **`passive_fill_then_adverse`** — THE HONEST FILL MODEL. Post a passive order AT THE TOUCH; over a
   `fill_window_s` window mark it FILLED the first second the mid crosses to/through the posted price
   (bid filled when mid falls to ≤ bid; symmetric for ask). On fill, the provider holds the inventory
   `hold_s` seconds, then marks the realized adverse mid move. Per-fill net = `earned_half_spread +
   signed_adverse_move`. Reports `fill_rate_ceiling`, `earn_half_spread_bps`, `realized_adverse_bps`,
   `net_per_fill_bps` (mean AND median).

## Pre-committed honesty caveats (flagged, not hidden — these are why bar-only LP is unfalsifiable)

- **Fill rate is a CEILING.** The mid-crosses-the-touch rule ignores queue position and assumes you are
  first in line. Real fill rate ≤ this. Consumers MUST treat `fill_rate_ceiling` as an upper bound.
- **Adverse selection is measured CONDITIONAL ON FILL and SIGNED** from the provider's post-fill
  inventory. You only fill on the side the market is moving toward, so unconditional |mid move| would
  overstate the earnable edge — the conditional signed move is the correct (worse) number.
- **No maker rebate / no fees / no exchange routing modeled.** A real venue adds a maker rebate
  (helps) and clearing/SEC fees (hurt). These shift the net by a name-independent constant the LP
  surface must add; the primitive isolates the spread-vs-adverse-selection core.
- **No trade tape join (yet).** The fill proxy is quote-only. A trade-anchored Lee-Ready fill (post,
  fill when a contra trade prints at/through the touch) is the higher-fidelity successor; flagged as the
  next-fidelity upgrade, not required for the WHERE-could-LP-work map this delivers.

## Predictions (pre-registered, before reading the result)

Standard market-microstructure theory (Glosten-Milgrom): the quoted spread compensates the provider for
adverse selection, so at competitive equilibrium net-of-adverse-selection capture should be ~0, and
TIGHTER-spread names (more competition, more informed flow relative to spread) should have net ≤ wider
names. I pre-register the qualitative prediction: **`net_per_fill` is monotone INCREASING in
`quoted_half_spread` across the core, near/below zero at the tightest names, and the magnitudes are
small (sub-2bps per fill).** A FLAT or INVERTED gradient, or large (>5bps) net at tight names, would be
a red flag of a look-ahead bug in the fill/adverse computation.

## Run

```
python3 lp_fill_primitives.py                 # full deep core x 20 sampled days
python3 lp_fill_primitives.py --symbols TSLA --n-days 40 --fill-window-s 30 --hold-s 120
```
Bounded, READ-ONLY (`/store/raw/quotes`), writes `lp_primitives_results.csv`. No container/fingerprint
touch.
