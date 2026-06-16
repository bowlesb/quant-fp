# W1 — Verdict: **KILL**

## Decision
**KILL** the cross-sectional price-momentum LIQUID portfolio L/S as a lead, on this 125-day window.

## Why (decisive controls, not vibes)
1. **Per-symbol demean kills it in 64/64 cells.** Subtracting each name's own mean forward H-day return —
   the control that separates a repeatable momentum RANKING from a persistent per-name LEVEL drift — turns
   every cell's L/S return NEGATIVE (−0.0095 to −0.145; demean_t < −2 in 28 cells, to t=−14). The positive
   gross is "long the names that already went up over a 6-month window," an unconditional level effect that
   does not generalize.
2. **The OOS-CI "passes" are artifacts.** The 6 cells whose OOS net-of-cost bootstrap CI clears zero ALL
   have negative demean and rest on 5–6 non-overlapping OOS rebalances (H10) — the same few-megacap drift,
   not an edge. A bootstrap over 5 numbers cannot certify a strategy.
3. **Cost was never the wall here.** Gross ≈ net1 ≈ net2 (turnover × ~7 bps ≈ a few bps/rebalance). The
   friction-wall-favorable shape kept friction negligible — but there is no underlying signal for low
   friction to preserve. This IS the sharp finding the hypothesis flagged: *the wall doesn't even need to
   bind; the diversified shape has no raw edge to begin with on this window.*

## Honest caveats (do NOT over-read the KILL)
- **125 days is genuinely too short for 3–12-month momentum.** Our truncated 21/42/63-day formations are a
  proxy, not classic 12-1 momentum. Non-overlapping rebalances leave only 2–12 OOS periods — underpowered.
- The KILL is for *this short-window, current-universe proxy*. It is NOT a claim that the canonical
  Jegadeesh-Titman factor is dead.

## Depth ask (conditional, LOW priority)
A clean 12-1 momentum test needs ≥18–24 months of daily closes so formation (12mo) + skip (1mo) + multiple
non-overlapping holding periods fit with real OOS power. **Recommendation: do NOT prioritize the deep-history
build.** The per-symbol demean reversing so hard (t down to −14) is a strong negative signal that the
apparent return is level/concentration, not rank-momentum — exactly McLean-Pontiff-style arbitraged-away
large-cap momentum. If deep history is acquired for OTHER reasons, re-run W1 as a cheap rider; do not stand
it up on its own.

## Pre-registration honored
Confidence was pre-set at ~30% that the liquid L/S clears net-of-cost OOS with bootstrap CI>0 and survives
demean+canary. Outcome: it does NOT survive the demean (the pre-committed decisive control). KILL as
pre-specified ("KILL: OOS net ≤ 0 or inside canary" — here it's the demean that fails, an even cleaner kill:
the raw OOS looks positive but is pure per-name level, which the demean proves).
