# MONTHLY LOW-TURNOVER FACTOR — cost-is-not-the-enemy surface (PRE-REGISTRATION + DESIGN)

**Author:** Modeller · **Date:** 2026-06-20 · **Status:** PRE-REGISTERED DESIGN — written BEFORE any
outcome. Deliverable this cycle = the design + pre-registration (run gated/sequenced with the Lead). The
H1 quote-spread re-test is a SEPARATE, gated one-shot (see §E) — NOT this surface.

## WHY THIS AVENUE (the meta-pattern pivot)

Across **6 settled negatives** (price ×2, order-flow, EDGAR+sector, 8-K event, weekly reversal) the
diagnosis is now unmistakable: **transaction COST kills cross-sectional direction/reversal at our scale.**
The weekly verdict sharpened it — weekly reversal was a REAL, clean, survivorship-robust, OOS-consistent
signal (shuffle-z 6.38, own-vol collapse 0.965, survives the −100% haircut) that died SOLELY because a
~32 bps/wk GROSS spread can't pay ~20 bps round-trip, with a **negative MEDIAN week** (the structural
killer: a thin-tail-positive-mean is not robust).

So the only honest pivot is a framing where **cost is structurally small by construction**, not fought
post-hoc. This surface attacks that directly: a **MONTHLY-rebalanced, TURNOVER-MINIMIZED** factor where the
edge (if any) accrues over a ~21-day hold while cost is paid ~once/month — and where **cost-awareness is a
FIRST-CLASS DESIGN PROPERTY** (a no-trade band / hysteresis on the signal so turnover is minimized by
design), not a post-hoc haircut. The pre-committed verdict metric is the **NET information ratio at
realistic cost with turnover penalized as a first-class term.**

## ⚠️ SUBSTRATE CONSTRAINT — HONEST UP FRONT

I verified there is **NO fundamentals data** (no value/quality/earnings tables; `sector_map` has only
sector/industry labels). So the canonical strongest low-turnover factors (value, quality) are **NOT
buildable** here. The implementable, genuinely-different, low-turnover candidates are **pure-price /
sector-relative** constructions. And the deep panel is **fully survivorship-biased** (verified prior:
[[project-weekly-horizon-survivorship]]) — so the design AGAIN picks signals where survivorship is a
controllable headwind, with the −100% delisting haircut as a standing gate. The honest prior is SKEPTICAL:
pure-price low-turnover factors are the most arbitraged, and our cost wall is real — a null is the likely
and still-valuable outcome (it would settle whether ANY low-turnover bar-only factor clears our cost).

## HYPOTHESES (pre-registered — 2, both LOW-TURNOVER, cost-minimized by construction)

### H1 — MONTHLY LOW-VOLATILITY / LOW-BETA (betting-against-beta), turnover-banded
**Claim:** low-realized-vol (or low-market-beta) names earn higher risk-adjusted forward MONTHLY returns —
the low-vol anomaly, the canonical low-turnover factor (vol/beta are persistent → the portfolio barely
turns over). The weekly version (prior H2) OOS-FLIPPED + died on cost; the MONTHLY horizon + a turnover
band is the genuinely-different test (4× lower rebalance, hysteresis so persistent names don't churn).
- **Signal:** `lowvol` = − trailing-60-trading-day realized daily-return vol (or − rolling 60d market-beta),
  as of each monthly rebalance (last trading day of the month, entered the next session's tradeable open
  ≥09:35 ET).
- **Turnover band (the cost-is-not-enemy mechanism):** a name only ENTERS/EXITS the long/short book when
  its signal rank crosses a HYSTERESIS band (e.g. enter top/bottom-quintile, exit only past the 30/70
  pctile) → the book is sticky, monthly turnover is minimized BY DESIGN. Turnover is measured + reported.
- **Target:** forward 21-trading-day return (next month) + forward monthly risk-adjusted return.

### H2 — SECTOR-RELATIVE MONTHLY MOMENTUM/REVERSAL, turnover-banded
**Claim:** within-sector relative strength has a monthly continuation (or reversal) the weekly horizon
couldn't monetize because of cost. Sector-relative (own minus sector-EW) removes the market + sector beta,
isolating the name-specific monthly drift, at low turnover with the band.
- **Signal:** `sector_rel_mom` = the name's trailing 21-day return minus its GICS-sector EW-mean 21-day
  return (#182 sector definition), as of the monthly rebalance.
- **Direction is pre-committed AGNOSTIC + tested both ways** (continuation vs reversal) but reported under
  multiple-comparisons control so testing both signs isn't a free shot.
- **Target:** forward 21-day sector-relative return (and raw forward return).

## DISCIPLINE (the full spine + the cost-as-first-class additions)
- **Tradeable entry:** monthly rebalance entered at the NEXT session's open ≥09:35 ET (never the
  month-end close → no close-to-close look-ahead); forward label from that tradeable entry.
- **Point-in-time liquid universe** (top-N trailing-ADV, reconstructed from bars; survivorship-caveated).
- **⭐ COST AS A FIRST-CLASS GATE (the new thing):** the verdict is the NET information ratio / net mean &
  MEDIAN monthly return AFTER cost = (per-name turnover that month) × (round-trip bps). Turnover is computed
  from the actual book changes under the hysteresis band, NOT assumed. Reported at 5/10 bps (and, when the
  quote tape lands, the real effective spread). A factor is "tradeable" only if NET median > 0 AND net IR
  clears a pre-set bar — the negative-median structural lesson from the weekly verdict is encoded as a
  PASS/FAIL gate, not a footnote.
- **Own-vol/size control** (the #187/#197/#205 lesson): partial out trailing vol + log-ADV; collapse ratio.
- **Shuffle baseline** (permute the forward label within each monthly cross-section) + **predict-zero**.
- **OOS:** walk-forward year-split (2016-2020 / 2021-2025) + per-year IR sign-consistency.
- **$1-floor + per-month symmetric winsorization**; **−30%/−100% DELISTING HAIRCUT** (standing gate).
- **BY-FDR** across all (hypothesis × sign × cost-level) cells — testing both momentum/reversal signs is in
  the FDR family.

## STOP CONDITIONS (pre-committed)
- A factor whose **NET MEDIAN monthly return > 0** + net IR clears the bar + survives shuffle + own-vol
  control + OOS + the −100% haircut = the FIRST cost-positive, median-robust edge → FLAG the Lead for a
  confirmatory replication BEFORE excitement. (The median gate is the bar the weekly reversal failed.)
- Net median ≤ 0 (positive mean only) → the SAME thin-tail failure as weekly reversal → honest null, NOT
  tradeable, reported as such (this is the likely outcome; it would confirm the cost wall extends to
  monthly bar-only factors).
- Dies at the signal/own-vol/OOS stage → honest null, cheaper to report. No post-hoc sign/threshold tuning;
  the both-signs test for H2 is in the FDR family, not a free re-roll.

## E. THE GATED QUOTE-SPREAD H1 RE-TEST (separate, one-shot — NOT this surface)
When DataIntegrity logs the deepened quote tape QUERYABLE (`quote_agg_1m` currently 0 rows / FETCH phase),
re-run the WEEKLY #205 H1 net-of-cost with the REAL mega-cap effective spread instead of the 5 bps proxy.
TEMPERED per the Lead: if real mega-cap spread is materially <5 bps the MEAN may lift, but the **negative
MEDIAN is the structural blocker** — a one-shot confirmatory test, not a chase. If the median stays negative
under real spreads, the weekly-reversal surface is SETTLED. (This is a 1-call follow-up on the existing
#205 panel, gated on the tape; it does not block this monthly design.)

## RUN PLAN (this cycle = design; run sequenced)
Design + pre-reg committed now. Build (`build_monthly.py`) reuses the #205 memory-bounded host-mounted
resumable daily-cache infra (same `daily_cache/<date>.parquet`, chunked-subprocess build — the lessons that
got #205 home). Screen (`screen.py`) adds the turnover-band book construction + the net-IR/net-median
cost-gate. Research-only: NO quantlib / NO fingerprint flip; READ-ONLY stores; bounded NAMED `--rm`
sandboxes (kill only by ID).
