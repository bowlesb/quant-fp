# #205 WEEKLY-REVERSAL QUOTE-SPREAD RE-TEST — VERDICT

**Date:** 2026-06-20 · One-shot, median-anchored (prereg §E of #205). Substrate: the #205 weekly_panel
(498 weeks) + the now-queryable quote tape (`/store/raw/quotes`, 379d, 2024-12-12→2026-06-18, 4,300 names,
liquid core complete). Reads READ-ONLY.

## TL;DR — SURFACE SETTLED. The real effective spread lifts the MEAN but the net MEDIAN stays NEGATIVE.

The #205 weekly reversal died on cost with a negative MEDIAN under a flat 5bps proxy. The pre-committed gate
(the coordinator's tempering note): the ONLY outcome that reopens the surface is the net MEDIAN crossing
POSITIVE under real effective spread — a better mean with median still <0 = settled. **The median does NOT
robustly cross positive.** Surface settled; moving to liquidity-provision.

## The real effective spread (the new data)
- Per-name effective HALF-spread = median RTH (ask−bid)/mid over 22 sampled quote days. Across the 805
  quote-covered liquid names: **median 4.03 bps** (p25 2.34, p75 6.84). So a liquidity-taking pair
  round-trip (each leg crosses a full spread = 2×half on entry+exit) ≈ **~16 bps**, genuinely BELOW the
  #205 flat-10bps proxy and around the flat-5bps half-spread assumption.

## The result — and the decisive robustness check
On the 51-week quote-covered window, with each name's REAL spread, the reversal L/S nets **mean +71 bps,
median +6.7 bps, win 51%** — the median ticks marginally positive. **BUT this is a SAMPLE-PERIOD artifact,
not a real-spread reopening.** Applying the real median spread across periods:

| period | weeks | net mean | net MEDIAN | win |
|---|---|---|---|---|
| **FULL 2016-2025** | 498 | +16 bps | **−18.6 bps** | 47% |
| recent quote window (≥2024-12) | 51 | +73 bps | **−2.2 bps** (≈0, coin-flip) | 49% |
| pre-2024-12 | 447 | +9 bps | **−21.5 bps** | 46% |

The net median is solidly NEGATIVE over the full panel (−18.6) and pre-2024 (−21.5), and only ~zero
(−2.2, with a 49% coin-flip win rate) in the favorable recent 51 weeks. The per-name +6.7 in the recent
window comes from the reversal book tilting to the very-tightest-spread mega-caps (below the 4bps median),
but the win rate there is still **51% — a coin flip**, and the effect does not generalize to any other
period. A median that is positive ONLY in one recent sub-window, ONLY marginally, ONLY at a coin-flip win
rate, while negative everywhere else, is NOT a robust tradeable edge — it is the same regime-dependence that
nulled the monthly low-vol (#212).

## Disposition — SETTLED, no escalation (the pre-committed outcome)
The real effective spread is genuinely tighter than the 5bps proxy and DOES lift the MEAN — but the
structural NEGATIVE MEDIAN holds across the full sample. Per the median-anchored gate: median does not
robustly cross positive → **surface SETTLED.** NO confirmatory-replication flag, NO promotion. The #205
weekly reversal is now closed: a real, clean, survivorship-robust signal whose net-of-cost median is
negative even under the real (tighter-than-proxy) effective spread.

This was exactly the trap to avoid: the raw single-window real-spread number (+6.7 median) would have
"reopened" the surface; the period decomposition shows it's a recent-window coin-flip, not a real edge.
Discipline held — a better mean did not tempt a second look.

## Method notes
- Pair round-trip cost = 2 full spreads = 4×half-spread (each leg crosses the half-spread on entry AND exit);
  per-name uses that name's real half-spread, flat models use the stated bps as the half-spread. (Reconciled
  carefully — an early intermediate check double-charged at 4×full; the committed model is 4×half = correct.)
- Window = #205 panel weeks ≥ 2024-12-12 (the first quote day), 51 weeks / 25,498 obs / 817 names, 805 with
  real quotes. Bounded NAMED `--rm` sandboxes, killed by ID. retest.py is the turn-key script.
