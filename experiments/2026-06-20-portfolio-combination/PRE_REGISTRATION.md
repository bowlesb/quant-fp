# PRE-REGISTRATION — multi-signal PORTFOLIO combination of the real-but-weak retained signals

**Date:** 2026-06-20  **Author:** Modeller  **Status:** GATE-READ REQUESTED (no numbers produced yet)
**The pivot:** stop hunting new single signals; test whether the AGGREGATE of the real-but-weak signals we
already found and retain clears net-of-(measured-Stage-1)-cost when no single leg does. This directly tests
the basket-noise constraint via diversification — the #1 move from the meta-synthesis (PR #288).

**⚠️ This is the single most p-hackable thing we will do.** Combining signals invites cherry-picking weights,
legs, and horizons until something clears. Every degree of freedom is LOCKED below before any run; the
combination METHOD is the fishing surface and it is fixed, not chosen after seeing results.

---

## 1. The hypothesis (one, falsifiable)
**H:** a principled, no-per-leg-tuning COMBINATION of the pre-committed real-but-weak signals produces an L/S
basket whose net-of-measured-cost return clears the FULL bar (net-$ up at conservative {2,5,10}% cuts AND
per-period NW-t ≥ 2 AND beats shuffle + predict-zero AND replicates disjoint) — even though each LEG
individually does not (each was a settled null/weak on this exact bar).

Honest prior: the legs may be CORRELATED (all partly priced by the trusted baseline, per #255), so the
portfolio may add nothing beyond the baseline — that is a clean, decisive answer too.

## 2. The PRE-COMMITTED signal set (the legs — exact list, CLOSED)
Each leg is a single per-name score at the decision instant. The set is fixed; no adding/dropping post-result.

| # | Leg | Concrete signal | Source | Horizon-native |
|---|-----|-----------------|--------|----------------|
| L1 | **weekly-reversal** | `−rev_1w` (neg trailing 5-day return) | bars (build_weekly) | WEEKLY (5d hold) |
| L2 | **swing_dc magnitude** | `dc_resp_chunk_slope` (the replicated roughness fingerprint) | swing_dc kernel | INTRADAY (30m) |
| L3 | **path/vol structure** | the trusted return-shape + vol groups' first PC (volatility, ohlc_vol, efficiency, return_dynamics, price_returns) — a single composite, NOT each feature as a leg | trusted store | INTRADAY (30m) |
| L4 | **quote-dynamics** | the G0a quote-dynamics composite (spread vol/trend, imbalance mean/trend, depth, intensity, staleness) | quote tape | INTRADAY (30m) |

NOTE on L3: the path/vol "signal" is a whole family; to avoid each feature being a fishing knob, L3 = ONE
composite = the first principal component of the standardized L3 feature block (a parameter-free reduction),
fit ON THE TRAIN FOLD only. This is locked: L3 is one leg, not a feature pile.

## 3. ⭐ HORIZON COMPATIBILITY (the real design choice — pre-committed, NOT improvised)
L1 is a 5-day-hold weekly signal; L2/L3/L4 are 30-min intraday signals. They predict DIFFERENT forward
windows; naively stacking them is meaningless. The pre-committed resolution — **run TWO separate portfolios,
each at its native horizon, plus one bridged test — all three fixed in advance:**

- **P-INTRADAY (primary):** combine L2 + L3 + L4 at the 30-min horizon (their native horizon), forward-30m
  cross-sectional excess label, the SAME substrate/entry as every prior intraday test (top-N liquid, 09:40
  ET, $1 floor, Stage-1 realized cost). This is the clean test of "do the intraday real-but-weak signals
  diversify each other?" — no horizon mismatch.
- **P-WEEKLY (secondary):** L1 ALONE is already the weekly test (#287) — it is NOT re-combined here (one leg
  is not a portfolio). Reported only as the reference the intraday portfolio is compared against; no new run.
- **P-BRIDGE (exploratory, OUTSIDE the confirmatory family):** L1 re-expressed at the 30-min horizon (does
  trailing-5d return predict the forward 30m?) and added to P-INTRADAY. This bridges the horizons but changes
  L1's meaning, so it is labelled EXPLORATORY and EXCLUDED from the BY-FDR family / the pass claim — reported
  for insight only, never as a "pass."

The confirmatory test is **P-INTRADAY (L2+L3+L4)**. L1's weekly result stands on its own (#287).

## 4. ⭐ THE COMBINATION METHOD (the fishing surface — LOCKED, no per-leg tuning)
Exactly TWO methods are run, both parameter-free, both fixed here (this is the entire combos count for FDR):

- **M1 — EQUAL-RISK-WEIGHT (primary):** standardize each leg's score to unit cross-sectional z per period
  (so no leg dominates by scale), then the combined score = the simple MEAN of the standardized legs. No
  fitted weights → zero per-leg tuning → not p-hackable. This is the principled "diversify equally" rule.
- **M2 — SINGLE WALK-FORWARD FIT (secondary):** ONE model (the harness GBM) trained on the train fold with
  ALL legs as inputs, predicting the forward label; the per-leg weighting is learned ONCE by the model, never
  hand-set. The frozen model scores the test fold (the standard harness path).

**N = 2 combination methods** (M1, M2) on the ONE confirmatory portfolio (P-INTRADAY). That is the FULL
multiple-testing count; BY-FDR is applied across N=2. No third method, no weight sweep, no leg-subset search.
If a leg-subset analysis is wanted later it is a SEPARATE pre-reg.

## 5. ⭐ SURVIVORSHIP — report BOTH ways (the Lead's requirement)
L1 (weekly-reversal) carries the survivorship caveat (buying censored survivor-losers). Since the
confirmatory portfolio P-INTRADAY does NOT include L1 (horizon mismatch, §3), P-INTRADAY is survivorship-CLEAN
by construction (intraday signals on a same-day panel — no multi-day delisting censoring). The survivorship-
both-ways reporting applies to the EXPLORATORY P-BRIDGE (which adds L1):
- P-BRIDGE reported WITH L1's −13bps/week survivorship haircut amortized to the 30m horizon AND WITHOUT it, so
  we see whether any P-BRIDGE lift leans on the survivorship-biased leg. Since P-BRIDGE is exploratory, this
  is for insight; the confirmatory P-INTRADAY is clean and needs no haircut.

This is the honest framing: the confirmatory test is the survivorship-clean intraday combination; L1's
survivorship-dependent dollars never enter the pass claim.

## 6. Anti-fooling spine (all pre-committed)
- **Walk-forward, purged** by the 30-min label horizon (the standard harness folds).
- **Stage-1 measured per-name cost** (the merged `realized_half_spread_bps`) — the portfolio must clear the
  ACCURATE cost, not the flat stub.
- **Shuffle baseline** (within-timestamp label permutation) + **predict-zero** — the portfolio curve must
  dominate both at every cut.
- **Per-period NW-t ≥ 2** of the basket return, NOT one-outlier-driven (report per-day distribution +
  max-day share — the cost-regrade / path-geom robustness check).
- **AUC + rank-IC up vs the baseline** (the portfolio must improve ranking, not just cost arithmetic).
- **Disjoint-window replication** required for any pass (discovery 2026-04-15..05-14 / replication
  05-15..06-12, the two natural halves of the 42-date trusted-coverage substrate).
- **BY-FDR q=0.10 across N=2** (M1, M2). One pass of two under a relaxed combination needs the correction.
- **Baseline comparator:** the portfolio is added to the FULL trusted baseline (the G2-sharpened standard) —
  it must beat baseline+nothing, i.e. show the legs add tradeable $ TOGETHER that they don't add singly.

## 7. The full pass bar (ALL must hold for a method to "pass")
1. net-$ up vs baseline at ALL of {2,5,10}% cuts (not a single-cut blip);
2. AUC AND rank-IC up vs baseline;
3. per-period NW-t ≥ 2, not one-outlier;
4. dominates shuffle + predict-zero at every cut;
5. survives BY-FDR across N=2;
6. replicates on the disjoint window.
A method missing ANY leg = NULL for that method.

## 8. PRE-COMMITTED outcome branches (inclusion-liberal disposition)
- **PASS (either M1 or M2, FDR-corrected, replicated):** the FIRST tradeable edge — the portfolio clears
  where legs don't. Flag the Lead for a confirmatory disjoint-period replication BEFORE any deploy talk.
- **NULL:** the combination doesn't clear yet — NOT "drop the signals." The legs stay INCLUDED/retained
  (inclusion is decoupled from $-value). A null here is decisive evidence that the real-but-weak signals are
  CORRELATED (the baseline already prices their shared structure), which itself sharpens the strategic
  picture (→ the edge really is the existing portfolio, and the move is deployment-hardening (c), not more
  combination). Report which: did the portfolio add gross ranking (legs diversify) but die on cost (basket
  still too thin) — or add nothing gross (legs redundant)?

## 9. What this is NOT
- NOT a weight sweep / leg-subset search (N=2 fixed methods, one fixed leg set).
- NOT a horizon mash-up in the confirmatory claim (P-INTRADAY is single-horizon; the bridge is exploratory).
- NOT leaning on the survivorship-biased leg (L1 is excluded from the confirmatory portfolio).
- NOT a single-cut or pre-cost "pass" (§7 requires the full bar at measured cost).
- NOT run yet — gate-read first. Lock the leg set (§2), the horizon design (§3), and the two combination
  methods (§4) with the Lead BEFORE any number. Send revisions now; they freeze at gate-read approval.
