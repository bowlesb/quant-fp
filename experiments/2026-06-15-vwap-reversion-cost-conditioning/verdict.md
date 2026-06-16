# Verdict — H1 (vwap_dev reversion, cost-conditioning) proof-of-loop

## Did the result match the pre-registered EXPECTED?
**Primary (carrier present + negative): YES — matched.**
Pooled within-minute `vwap_deviation_30m`→forward-return rank-IC is **NEGATIVE** at both horizons:
−0.048 (t=−5.1) at H=5 and −0.028 (t=−2.7) at H=15. This is squarely inside the pre-committed
−0.01…−0.05 band. The reversion carrier is present and negative in a fresh live RTH session, and the
leakage canary is ~0 (clean, 10-seed mean +0.0005). The key falsifier (positive/zero pooled IC) did
**not** trigger. ✅

**Secondary (the load-bearing one for H1): FAILED in the AGAINST-H1 direction.**
The reversion is **clearly STRONGER in the ILLIQUID half** at both horizons:
- H=5: low-liq |IC| 0.0649 vs high-liq 0.0315 → **2.06×**
- H=15: low-liq |IC| 0.0439 vs high-liq 0.0110 → **4.01×**
Both exceed the pre-registered illiquid > 2× liquid threshold that was committed as **evidence AGAINST
H1**. The high-liq half is weak (H=15 high-liq t=−0.90, not even significant); essentially all of the
reversion lives in the illiquid half — i.e. exactly where trading cost is worst. This is the structural
failure mode H1's cost-conditioning thesis was meant to avoid.

## Carrier present + negative?
**Yes.** Reversion confirmed, negative, significant pooled, canary-clean.

## Illiquid-stronger (evidence against H1)?
**Yes — and consistently** (2.06× at H=5, 4.01× at H=15, robust across both horizons in the same session).
This is the one outcome the pre-registration said should down-rank H1.

## H1 status: **KILLED** (down-ranked)
The probe found exactly the falsifying pattern for H1: the proven carrier is real but **concentrated in
illiquid names**, so a liquidity-gated + hysteresis variant gates AWAY most of the signal. The signal
lives where cost is worst; cost-conditioning cannot rescue economics by selecting liquid names — there is
little reversion there to harvest. The one honest hedge: this is a **single noisy Monday session** (per-
minute IC std ~0.08, n=60–68 minutes of one day). The pre-registration allowed "ambiguous/blocked on
multi-day panel" as a non-kill outcome — but here the illiquid-stronger sign is **clear and directionally
consistent across two horizons**, which is precisely the stated down-rank trigger. So: KILLED, not merely
blocked.

## One-line next step
Down-rank H1; before fully retiring it, run the identical liquidity-split IC on a 5–10 day panel once the
multi-day store exists to confirm the illiquid-concentration is stable (not a single-Monday artifact) —
but do NOT invest in a liquidity-gated variant on the strength of this carrier.
