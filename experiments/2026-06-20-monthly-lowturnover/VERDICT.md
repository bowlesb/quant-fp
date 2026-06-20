# MONTHLY LOW-TURNOVER FACTOR — VERDICT

**Date:** 2026-06-20 · **Pre-reg:** `prereg.md` (before outcomes) · **Panel:** 57,800 obs, **116 monthly
rebalances, 2016→2025, 1,362 symbols**, top-500 trailing-ADV liquid universe (point-in-time), tradeable
next-session-open ≥09:35 ET entry, $1-floor + per-month winsor, 21 disappeared. Code on the PR #212 branch.
Results: `screen_results.csv` / `screen_console.txt`.

## TL;DR — CLEAN NULL. No low-turnover bar-only factor clears the tradeable gate; the cost-isn't-the-enemy thesis is settled for pure-price factors.

The turnover-minimized-by-construction design worked exactly as intended (the hysteresis band held low-vol
turnover to 0.38), and the net-MEDIAN>0 PASS/FAIL gate was applied. **No factor passes.** Decisively:
low-vol has a real, size-independent cross-sectional IC but is **OOS-regime-flipping AND its L/S spread is
GROSS-negative over the decade** — it doesn't clear zero, let alone cost; sector-relative momentum is a
null (FDR-fails, full turnover, cost-negative both signs). This settles whether ANY low-turnover bar-only
factor (the only kind buildable without fundamentals) clears our cost: it does not.

## H1 — MONTHLY LOW-VOL / LOW-BETA: real IC, but OOS-flips + gross-negative spread + cost-negative

| gate | number | verdict |
|---|---|---|
| raw monthly rank-IC | +0.0143 (NW-t 0.61) | weak signal present |
| shuffle-z | 3.15 | ✅ vs the within-month label-permute null |
| BY-FDR | survives | ✅ |
| size control collapse | **1.019** (partial 0.0146 ≈ raw) | ✅ NOT a size tilt — a genuine low-vol IC |
| **OOS year-split** (≤2020 / ≥2021) | **FLIP −0.027 / +0.055** | ❌ regime-dependent; low-vol underperformed pre-2021, outperformed post — sign NOT stable |
| turnover (hysteresis band) | **0.38** | ✅ the cost-minimization design worked |
| **gross L/S spread** | **−21 bps/month** | ❌ NEGATIVE even before cost (the OOS flip averages to a losing book over the decade) |
| net-of-cost @5bps | mean −25, **median −42**, win 49% | ❌ |
| −100% delisting haircut | barely moves (median +11 = noise on a losing book) | survivorship NOT the issue |

**The kill is two-fold and deeper than cost:** (1) the L/S spread is GROSS-negative over the full 10-year
panel because the factor's sign OOS-FLIPS (low-vol is a regime trade, not a stable premium here); (2) even
in the favorable post-2021 sub-period the edge is too weak to clear cost. The IC is real and size-clean, but
a sign-unstable, gross-negative factor is not tradeable at any cost level. This is NOT the #205 "real signal
killed only by cost" story — here the signal itself is regime-conditional.

## H2 — SECTOR-RELATIVE MONTHLY MOMENTUM/REVERSAL: null

IC ±0.0058, shuffle-z ±1.30 — **does NOT survive FDR** (both signs tested, in the family). Turnover ~1.0
(sec-relative momentum churns fully — NOT low-turnover, the band can't make a fast signal sticky). Net
median negative both signs. A clean null on both the signal and the cost axes.

## Disposition — clean null, NO escalation (the pre-committed outcome)

Per the stop condition: **no factor has net-MEDIAN > 0**, so none is tradeable; the run dies at the
signal-stability / gross-spread / cost gates. Honest null. **NO confirmatory-replication flag** (the stop
condition only escalates a net-median-positive survivor). NO promotion. The 7th settled negative.

WHAT IT SETTLES (the value): this is the cost-isn't-the-enemy thesis, TESTED with cost minimized by
construction (the hysteresis band) and applied to the only low-turnover factors buildable on our substrate
(pure-price, no fundamentals) — and it is a clean null. Combined with the 6 prior negatives, the conclusion
is sharp: **at our scale, on bar-only data, neither cross-sectional direction/reversal NOR low-turnover
pure-price factors clear cost.** The two remaining honest avenues both need data we now have: (a) the
quote-spread re-test of the #205 weekly reversal (real effective spread vs the 5bps proxy — gated, now
unblocked), and (b) a liquidity-PROVISION surface (earn the spread, honest fill modeling off real bid/ask —
the avenue that needs queryable quotes, now available). Both are the next threads.

## Method / infra notes (for the adversarial auditor)
- Point-in-time trailing-21d-ADV universe; tradeable next-session-open entry (no month-end close-to-close
  look-ahead); ET-anchored Int32-cast daily aggregation (the #197 DST/Int8 fix).
- low-vol control = **size-only** (vol60 would be a perfect-collinearity self-control since lowvol = −vol60
  — caught + fixed in validation). sec_rel_mom control = vol60 + size. Shuffle = 200-iter within-month
  label permute. Turnover-banded book = hysteresis (enter quintile / exit past 30-70 pctile); turnover from
  ACTUAL book changes, normalized by avg book size (≤1). −30%/−100% delisting haircut on the 21 disappeared.
- INFRA reused from #205 (the lessons that got it home): host-mounted resumable partition cache (2514
  day-files, NOT ephemeral) + chunked-subprocess build (anon RSS bounded ~0.26 GiB; docker-stats MEM counts
  reclaimable page cache) + .RUN_COMPLETE marker. The run completed clean in one detached named container.
