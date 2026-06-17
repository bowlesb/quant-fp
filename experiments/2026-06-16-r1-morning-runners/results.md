# R1 small-cap morning runners — Stage 1 (bars-only) RESULTS

Run: `characterize.py` (parallel, 7,682 symbols × ~379 trading days of 1-min bars,
2024-12-11 → 2026-06-17). Output: `runner_events.parquet` (1,571 rows, 963 symbols).
Runner-day = prev RTH close ∈ [$2,$20] AND early_move (max first-30-min high / prev_close − 1)
AND first-30-min volume surge (f30 vol / trailing-20d median f30 vol).

## Event counts (runner-days / unique symbols)
| early_move ≥ | surge ≥2 | surge ≥3 | surge ≥5 |
|---|---|---|---|
| 0.30 | 1571 / 963 | 1534 / 939 | 1473 / 916 |
| 0.50 | 657 / 479 | **643 / 468** | 624 / 451 |
| 1.00 | 211 / 185 | 205 / 180 | 203 / 178 |
| 2.00 | 76 / 73 | 72 / 69 | 72 / 69 |

Event rate is ample (~1.7 CORE runner-days/trading-day). The vol-surge threshold barely
prunes — early_move (the price move) is the binding selector; surge is nearly collinear with
a big early move (a +50% move in 30 min is mechanically a volume event).

## CORE cell (early ≥0.50, surge ≥3): 643 days / 468 syms
- early_move pctiles 10/50/90: **0.54 / 0.78 / 2.17** (i.e. +54% / +78% / +217% off prev close)
- prev_close pctiles 10/50/90: **$2.21 / $4.00 / $10.65** (genuinely small-cap)
- runner-day $vol 10/50/90: **$13.3M / $153.6M / $729.9M** — LIQUID on the runner day (capacity real)

### The runner FADES — strong, consistent reversal at BOTH horizons
- **Intraday** (close vs first-30-min high): median **−17.8%**; only **19%** close at/above the
  f30 high; **65%** fade >10% off the high. The 30-min high is, in the median, the high-water mark.
- **Multi-day** (RTH-close → fwd close), per-symbol, n≈630:
  - fwd 1d: median **−6.3%**, frac up **32%**
  - fwd 3d: median **−9.8%**, frac up **32%**
  - fwd 5d: median **−13.9%**, frac up **30%**
  Monotonic, deepening reversal. Only ~30% of runners are still up 5 days later.

Top examples (early_move): CWD +2127% ($2.15), DMRA +752%, NAKA +708%, ROLR +648%, BNC +607%,
ABVX +596%, SBET +550% — the canonical low-float small-cap squeeze/pump pattern.

## Interpretation
The tradeable shape is **FADE the runner, not chase it.** The continuation hypothesis (R1 as
stated — "predict continuation vs reversal") resolves overwhelmingly to REVERSAL in the
unconditional CORE set: intraday give-back AND multi-day mean-reversion are both large and
consistent. The runner peaks near the first-30-min high and bleeds for days.

This sidesteps the friction wall the way the lead memo predicted: a 6–14% multi-day drop and a
~18% intraday give-back dwarf even a wide small-cap spread. BUT the binding constraint is now
**EXECUTION REALITY**, exactly as pre-registered — and for a SHORT it is severe:
- **Borrow.** These are hard-to-borrow / no-borrow low-float names; the multi-day short may be
  unborrowable or carry punitive fees that eat the −6 to −14% edge.
- **LULD halts / limit-up bands** interrupt the intraday fade entry; you cannot short into a halt.
- **The 30-min high is not a fillable short price** (it's the peak tick) — entry must be a
  tradeable post-peak price, and the realized give-back from a TRADEABLE entry (not the high) is
  the number that matters. Stage 1 measured close-vs-high, which OVERSTATES a shortable edge.

## Verdict
- **As a standalone short STRATEGY: PROMISING but NOT YET CERTIFIED — gated on EXECUTION REALITY**
  (borrow availability/cost + LULD-halt-aware tradeable entry + per-trade bootstrap on the realized,
  not peak-anchored, give-back). This is Stage 2.
- **As a FEATURE: STRONG, ship it.** The runner-state (early_move, vol_surge, gap, is_runner, and
  the post-peak give-back) is a real, parity-true, point-in-time signal with a large, consistent
  forward sign. It is non-redundant (no existing group encodes "name ran +50% in the first 30 min
  off a $2–20 base on a volume surge") and is emphatically not noise. A model gains a clean
  conditioning variable for the small-cap reversal regime. → batch-1b candidate **F9 runner_state**.

## Next (Stage 2, gated)
1. Selective tick backfill of the 468 CORE runner-day symbols (PR #75 `selective_backfill --symbols`)
   — most are already inside the 63-day trade window; older ones need backfill.
2. Re-measure the give-back from a **tradeable post-peak entry** (≥1–5 min after the f30 high,
   LULD/halt-filtered), per-trade non-overlapping bootstrap, shuffle-canary.
3. Borrow-reality gate (can we actually short these / at what fee).
4. GPU job 1: sequence model on the first-30-min tick/bar path → predict the give-back magnitude
   (the few continuation cases vs the reversal majority) — the ML refinement on top of the
   unconditional fade.
