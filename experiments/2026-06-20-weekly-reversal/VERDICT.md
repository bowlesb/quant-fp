# Weekly-reversal — VERDICT: NULL on the full bar, but the cleanest REAL signal yet (survivorship-killed)

**Date:** 2026-06-20  **Panel:** 2018-2025, 397 weeks, 397k obs, 2,612 distinct syms (point-in-time top-1000
ADV/week), disappeared=36, realized Stage-1 cost on the 2026 sub-window (6% of rows; deep years use the
conservative 5bps bar proxy). Discovery <2022 (198 wk) / Replication >=2022 (199 wk). Haircut −12.7bps/wk
base (P_delist 0.23%/wk × LGD −55%), −23bps/wk at −100% stress.

## The signal IS real at the IC level (the first clean cross-sectional signal in the whole hunt)
| | DISCOVERY (<2022) | REPLICATION (>=2022) |
|---|---|---|
| weekly rank-IC mean | **+0.0250** | **+0.0250** |
| rank-IC NW-t | +1.90 | **+2.40** |
| shuffle-z | **+10.78** | **+11.02** |
| partial-IC (own-vol+size) | **+0.0246** | **+0.0250** |

Reversal rank-IC is +0.025 in BOTH windows, sign-consistent, dominates the shuffle null by ~11σ, and FULLY
survives the own-vol/size control (partial-IC ≈ raw IC — it is NOT just small-illiquid names bouncing, the
10/13-survivor killer). This is a genuinely robust gross cross-sectional signal — the first in the hunt.

## But it FAILS the tradeable pass bar — on the two legs the gate exists for
| L/S basket (net) | DISCOVERY mean/wk | NW-t | REPLICATION mean/wk | NW-t |
|---|---|---|---|---|
| gross | +35.2 bps | +0.78 | +19.2 bps | +0.93 |
| net Stage-1 cost | +35.2 | +0.78 | +11.5 | +0.55 |
| **+ BASE −13bps haircut** | **+22.5** | **+0.50** | **−1.2** | **−0.06** |
| + −100% stress | +12.2 | +0.27 | −11.5 | −0.55 |

Two decisive failures, both the ones the gate was built for:
1. **Per-week NW-t < 2 on the BASKET** (0.50 / −0.06 after base haircut). The rank-IC is significant
   (t=1.9/2.4) but the dollar-weighted decile L/S return is too noisy week-to-week to clear t≥2 — a real but
   weak-and-noisy effect, not a tradeable basket.
2. **The SURVIVORSHIP haircut is load-bearing in replication:** net-cost +11.5bps → base-haircut **−1.2bps
   (sign FLIPS)**. The −13bps/wk calibrated loser-leg drag exactly consumes the recent edge. This is the
   bias the Lead made me gate for, doing precisely its job — without it, replication would have looked like a
   +11.5bps "edge" that is actually an artifact of buying censored survivor-losers.

## Verdict: NULL (full bar not cleared), sign-consistent, survivorship-killed in the tradeable window
- Discovery: base-haircut net-positive (+22.5) but NW-t 0.50 < 2 → fails.
- Replication: base-haircut sign-negative (−1.2) → fails.
- Both fail; the −100% stress (band-end) is reported but not the gate (per Lead): discovery holds sign under
  −100% (+12.2), replication does not (−11.5) — moot since base already fails.

## Disposition (Ben's principle — what-to-TRADE, not what-to-store)
A NULL here = the current model does not TRADE weekly-reversal profitably yet on this survivors-only
substrate — NOT that `rev_1w` is worthless. **The reversal IC is real, survivorship-independent, and
shuffle-clean; `rev_1w` and the weekly features stay INCLUDED/retained.** The edge died on (a) per-week
basket noise and (b) the survivorship haircut in the tradeable window. The honest, decisive follow-up — flag,
do not chase: **acquire a delisting-inclusive (CRSP-style) universe.** On a survivorship-correct panel the
loser leg would include the censored losers, so the haircut would be an in-sample MEASUREMENT, not an
external estimate — and we could test whether the +0.025 IC is tradeable once the bias is removed by data
rather than charged by assumption. That is the single highest-value data acquisition the hunt has surfaced.

## Why this is the most informative null of the hunt
Every prior null died on gross signal OR cost. This one has a CLEAN, replicated, own-vol-independent gross
signal (IC +0.025, 11σ vs shuffle) that dies specifically on (1) basket noise and (2) the survivorship bias —
exactly quantifying that the reversal anomaly is real but (on a survivors-only panel, at our turnover) not
tradeable net. The calibrated haircut earned its keep: it was the difference between a false +11.5bps "edge"
and the honest −1.2bps truth in replication.
