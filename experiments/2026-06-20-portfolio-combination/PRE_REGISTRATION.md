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

**⭐ Option A (Lead-approved, substrate-driven):** the deep S-INTRADAY is built from RAW BARS (L2+L3, both
bar-derived → the full 397-week common span with S-WEEKLY). **L4 quote-dynamics is DROPPED from the
confirmatory headline** (quote-tape-bound to 2026-03+ AND already a confirmed cost-killed null #268/#275 — no
loss); it is kept ONLY as a recent-window (2026-04..06) SECONDARY, never in the deep headline pass claim.

| # | Leg | Concrete signal | Source | Horizon-native | In confirmatory? |
|---|-----|-----------------|--------|----------------|------------------|
| L1 | **weekly-reversal** | `−rev_1w` (neg trailing 5-day return) | raw bars (build_weekly) | WEEKLY (5d hold) | YES (S-WEEKLY) |
| L2 | **swing_dc magnitude** | `dc_resp_chunk_slope` (the replicated roughness fingerprint) | swing_dc kernel on minute bars (DEEP) | DAILY (intraday-family) | YES (S-INTRADAY) |
| L3 | **path/vol structure** | first PC of the bar-derived return-shape + vol block (trailing realized vol, range, multi-horizon returns, efficiency) computed FROM RAW BARS — ONE composite, fit on the train fold | raw bars (DEEP) | DAILY (intraday-family) | YES (S-INTRADAY) |
| L4 | **quote-dynamics** | the G0a quote-dynamics composite | quote tape (recent only) | 30m | NO — recent-window secondary only |

NOTE on L3: the path/vol "signal" is a whole family; to avoid each feature being a fishing knob, L3 = ONE
composite = the first principal component of the standardized L3 bar-derived block (a parameter-free
reduction), fit ON THE TRAIN FOLD only. Locked: L3 is one leg, not a feature pile.

NOTE on S-INTRADAY horizon: deep history has minute bars but the tractable deep cross-section is DAILY-
rebalanced (score each day from trailing bar structure, book a decile L/S held to the next tradeable day).
This is the deepest powered form of the intraday-family stream; its P&L is daily → aggregated to weekly to
align with S-WEEKLY (the §3 common-frequency rule). The recent-window L4 secondary keeps the true 30m form.

## 3. ⭐ HORIZON COMPATIBILITY — COMBINE AT THE RETURN-STREAM LEVEL (Lead resolution, pre-committed)
The horizon mismatch is real ONLY for SIGNAL-level averaging (you cannot average a 5-day-hold score and a
30-min score — they predict different forward windows). The textbook fix is to combine at the
STRATEGY/RETURN-STREAM level: run each signal as its OWN native-horizon strategy, produce its P&L stream, and
risk-allocate ACROSS the streams. Diversification then comes from the LOW CORRELATION of the streams
(different horizons → ~uncorrelated P&L → the combined Sharpe/NW-t can clear when no single stream does). This
IS the actual diversification hypothesis, and it handles survivorship cleanly.

**Two streams:**
- **S-INTRADAY:** the intraday-composite strategy = L2 + L3 + L4 combined at SIGNAL level at their native 30m
  horizon (they share a horizon, so signal-level is correct here), booked as a decile L/S → one P&L value per
  30m-sample period, on the trusted intraday substrate (top-N liquid, 09:40 ET, $1 floor, Stage-1 cost).
- **S-WEEKLY:** the weekly-reversal strategy = L1 (`−rev_1w`) booked as a decile L/S at weekly cadence → one
  P&L value per week (the #287 strategy, point-in-time top-1000 ADV universe, tradeable Mon≥09:35 entry).

**Common eval frequency (pre-committed):** aggregate the S-INTRADAY P&L to WEEKLY (sum the within-week 30m
period P&L into one weekly return) — aligning to the LOWER-frequency leg is the principled common frequency.
Both streams are then a weekly return series on the same weekly calendar; combine across them.

**The combined portfolio (PRIMARY/confirmatory) = P-MULTI-HORIZON:** risk-allocate across {S-WEEKLY,
S-INTRADAY} via the §4 methods. The diversification benefit is measured as the combined weekly stream's
Sharpe / NW-t vs each single stream's — does the combination clear when neither stream does alone?

**SECONDARY = P-INTRADAY (signal-level, 30m):** the L2+L3+L4 signal-level combination on its own (the prior
design) — kept because it cleanly answers "are the intraday signals baseline-priced?" (likely null per #255,
still decisive/useful), but it is NOT the headline. The headline is P-MULTI-HORIZON.

## 4. ⭐ THE COMBINATION METHOD (the fishing surface — LOCKED, no per-leg tuning)
Exactly TWO methods are run, both parameter-free, both fixed here (this is the entire combos count for FDR):

- **M1 — EQUAL-RISK-WEIGHT (primary):** scale each STREAM to unit realized volatility (equal risk
  contribution), then the combined stream = the simple MEAN of the vol-scaled streams. (For the secondary
  signal-level P-INTRADAY: z-standardize each leg's score per period, mean.) No fitted weights → zero tuning →
  not p-hackable. The principled "diversify equally by risk" rule.
- **M2 — SINGLE WALK-FORWARD FIT (secondary):** ONE model fits the across-stream (or across-leg) weighting
  ONCE on the train fold, never hand-set. For P-MULTI-HORIZON: a single ridge fit of the combined weekly
  stream on the two stream returns (train fold), frozen and applied to the test fold. For P-INTRADAY: the
  harness GBM on all legs (the standard path).

**MULTIPLE-TESTING COUNT: N = 4** = {M1, M2} × {P-MULTI-HORIZON (primary), P-INTRADAY (secondary)}. That is
the FULL combos count; **BY-FDR is applied across N=4**. No third method, no weight sweep, no leg-subset
search. A leg-subset or extra-method analysis later is a SEPARATE pre-reg.

## 5. ⭐ SURVIVORSHIP — report BOTH ways, the PASS CLAIM is on the HONEST-cost stream (Lead requirement)
The S-WEEKLY stream (L1) carries the survivorship caveat (buying censored survivor-losers). The both-ways
reporting now does the heavy lifting on the PRIMARY P-MULTI-HORIZON pass claim:
- The S-WEEKLY stream's weekly returns are computed BOTH ways: **WITH the −13bps/week survivorship haircut
  applied to its loser leg** AND **WITHOUT**. P-MULTI-HORIZON is therefore evaluated twice (haircut-on /
  haircut-off).
- **THE PASS CLAIM IS ON THE HONEST-COST (haircut-APPLIED) combined stream.** If P-MULTI-HORIZON clears only
  with the haircut OFF and fails with it ON → the diversification benefit LEANS ON the survivorship-biased
  leg → flagged survivorship-dependent, routes to the delisting-data track, NOT banked.
- The secondary P-INTRADAY is survivorship-clean by construction (intraday signals, same-day panel — no
  multi-day delisting censoring); no haircut applies there.

So the headline diversification test (P-MULTI-HORIZON) genuinely includes the weekly leg's low-correlation
P&L — where the diversification hope lives — while the survivorship-honest-cost gate keeps the pass claim
unimpeachable: a banked pass MUST clear with the weekly leg charged its full survivorship haircut.

## 6. Anti-fooling spine (all pre-committed)
- **Walk-forward, purged** — each stream's strategy is fit/scored walk-forward at its native horizon
  (S-INTRADAY by the 30m harness folds; S-WEEKLY by the #287 weekly folds); the M2 cross-stream fit is itself
  walk-forward on the weekly calendar (no test-period stream return informs its own weight).
- **Stage-1 measured per-name cost** (the merged `realized_half_spread_bps`) charged in EACH stream's P&L
  before combination — both streams are net-of-accurate-cost, not pre-cost.
- **Shuffle baseline** (permute each stream's period labels / forward returns) + **predict-zero** — the
  combined stream's Sharpe/NW-t must dominate both.
- **Per-WEEK NW-t ≥ 2** of the COMBINED weekly stream return, NOT one-outlier-driven (report the per-week
  distribution + max-week share — the cost-regrade / weekly-reversal robustness check). For the secondary
  signal-level P-INTRADAY, per-period NW-t on its threshold-cut basket.
- **Diversification must be REAL, not arithmetic:** report the cross-stream return CORRELATION (the
  diversification only works if it is low) and the combined Sharpe/NW-t **vs each single stream's** — the
  combination must IMPROVE on the better single stream, not just average two streams.
- **Disjoint-window replication** required for any pass (split the common weekly span into two non-overlapping
  halves; pass must hold sign + Sharpe/NW-t + post-survivorship-haircut on both).
- **BY-FDR q=0.10 across N=4** ({M1,M2} × {P-MULTI-HORIZON, P-INTRADAY}).
- **Single-stream comparator:** the combined stream must beat BOTH single streams (S-WEEKLY-alone is #287's
  result, S-INTRADAY-alone is the signal-level P-INTRADAY) — i.e. the diversification adds risk-adjusted
  return neither stream has alone.

## 7. The full pass bar (ALL must hold for a method×arm to "pass")
PRIMARY (P-MULTI-HORIZON, the headline):
1. combined weekly stream Sharpe/NW-t ≥ 2 **on the HONEST-cost (survivorship-haircut-APPLIED) stream**;
2. combined Sharpe/NW-t IMPROVES on the better single stream (real diversification, not averaging);
3. cross-stream correlation is low (the diversification mechanism is present, not assumed);
4. dominates shuffle + predict-zero;
5. replicates on the disjoint weekly window;
6. survives BY-FDR across N=4.
SECONDARY (P-INTRADAY, signal-level): net-$ up vs baseline at ALL {2,5,10}% cuts + AUC/rankIC up + per-period
NW-t ≥ 2 + beats shuffle/predict-zero + replicates (the standard intraday bar).
A method×arm missing ANY of its legs = NULL.

## 8. PRE-COMMITTED outcome branches (inclusion-liberal disposition)
- **PASS (M1 or M2 on P-MULTI-HORIZON, FDR-corrected, replicated, on the HONEST-cost stream):** the FIRST
  tradeable edge — diversification across horizons clears where no single stream does, AND it survives the
  weekly leg's full survivorship haircut. Flag the Lead for a confirmatory disjoint-period replication BEFORE
  any deploy talk.
- **PASS only with survivorship-haircut OFF:** the diversification leans on the survivorship-biased weekly
  leg → NOT banked → routes to the delisting-data acquisition track (move (a)), reported as survivorship-
  dependent.
- **NULL:** the combination doesn't clear yet — NOT "drop the signals." Legs/streams stay INCLUDED/retained
  (inclusion decoupled from $). Report WHICH null: (i) streams too CORRELATED (no diversification — the edge
  really is the existing portfolio → deployment-hardening (c)); (ii) diversification real (low corr, combined
  Sharpe up) but still sub-threshold (basket/streams too thin → more streams / longer span needed); (iii)
  clears gross but dies on cost/survivorship-haircut. Each routes differently — name it.

## 9. What this is NOT
- NOT a weight sweep / leg-subset / horizon search (N=4 fixed method×arm cells, one fixed leg set, the common
  weekly eval frequency fixed in §3).
- NOT a signal-level horizon mash-up (the primary combines RETURN STREAMS at the stream level — the correct
  way to combine different-horizon strategies).
- NOT a pass that leans on survivorship (the PASS CLAIM is on the haircut-APPLIED stream; haircut-off-only =
  flagged, not banked).
- NOT a pre-cost or arithmetic "pass" (§7 requires net-of-measured-cost AND improvement over the better single
  stream AND low cross-stream correlation).
- This pre-reg is REVISED per the Lead's return-stream resolution; the methodology is now clear and locked →
  RUN on this (no second gate-read round needed per the Lead's steer). Report BOTH arms, BOTH-ways.
