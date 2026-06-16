# H4 Verdict: Split POST-event Drift

**Date:** 2026-06-16
**Pre-committed thresholds:** KEEP = liquid-tertile canary-clear, sign-correct, N>=20.
UNDERPOWERED = N<20 in liquid cell (per pre-registration in hypothesis.md).

---

## REVERSE SPLITS — verdict per cell

| Cell | N_events | Verdict | Rationale |
|------|----------|---------|-----------|
| Full universe, all horizons | 244–297 | STRONG (illiquid/mid) | t=-2.38 to -3.55, canary-clear, demean survives |
| Illiquid T1, h=1–20d | 147–179 | STRONG but untradeable | Massive alpha (-5% to -32%), canary-clear, but $208K/day cap = untradeable |
| Mid T2, h=1–10d | 93–114 | SUGGESTIVE | Strong t (-2.13 to -4.37), canary-clear; mid-liquidity is borderline tradeable |
| **Liquid T3, h=1–5d** | **4** | **UNDERPOWERED** | **Direction correct (negative, -13 to -15%), canary-clear at h=1–5d, but N=4 << 20. Pre-committed: report as "directionally suggestive, needs H8 deep-history backfill."** |
| Liquid T3, h=10–20d | 4 | UNDERPOWERED | Same N; canary fails at h=20d |

**Reverse split overall verdict: UNDERPOWERED for the LIQUID-TRADEABILITY gate.**
The drift is real and extremely strong in illiquid + mid tiers (full-universe t up to -4.57),
but only 4 of 312 reverse splits (1.3%) occur in liquid-tier names. This is structural:
reverse splits are distress events; distressed companies are inherently illiquid. A liquid
reverse-split cohort cannot exist in adequate size within the 6-month bars window.

---

## FORWARD SPLITS — verdict per cell

| Cell | N_events | Verdict | Rationale |
|------|----------|---------|-----------|
| Full universe, all horizons | 16 | UNDERPOWERED | N=16 total; all horizons |
| Illiquid T1 | 1 | UNTESTABLE | 1 event; NaN stats |
| Mid T2 | 6 | UNDERPOWERED | N=6; t<1 at all horizons |
| Liquid T3 | 9 | UNDERPOWERED | N=9; t<0.5 at all horizons |

**Forward split overall verdict: UNDERPOWERED — completely untestable at 6-month depth.**
Only 17 forward splits in the bars window, 9 in liquid tier. Even the sign is wrong
(negative alpha at all horizons, predicted positive) — but this is meaningless with N=9.
There is no basis for KEEP or KILL; needs the H8 deep-split backfill.

---

## The meta-pattern assessment

H4 confirms the cycle meta-pattern: **every event-driven signal is illiquid-concentrated.**
- Reverse-split drift: overwhelmingly in the bottom-tercile $208K/day universe.
- Liquid-tier reverse splits: 4 out of 312 events = structurally near-empty by construction.
- This is not a data problem — it reflects the fundamental economics of who does reverse splits.

The 4 liquid reverse-split events show direction-correct, canary-clearing drift at h=1–5d
(-13 to -15%, t=-2.25 to -2.96). This is directionally interesting. But with N=4 and
pre-committed bar of N>=20, this is "directionally suggestive, needs H8."

---

## Net verdict summary

| Split type | Direction predicted | Full-universe verdict | Liquid verdict |
|------------|--------------------|-----------------------|----------------|
| Reverse | Negative | STRONG (real, robust) | UNDERPOWERED (N=4, needs H8) |
| Forward | Positive | UNDERPOWERED (N=16) | UNDERPOWERED (N=9, needs H8) |

**Action:** Both split types require the H8 deep-split backfill (delisted names,
longer history) before any liquid-tier verdict is possible. The reverse-split drift
in illiquid/mid names is documented but not tradeable in the liquid-tier target universe.
Escalate to H8 for backfill rather than promoting either split type as a feature KEEP.
