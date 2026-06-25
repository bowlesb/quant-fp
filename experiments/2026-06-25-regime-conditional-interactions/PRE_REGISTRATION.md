# PRE-REGISTRATION — Regime-conditional feature interactions (the last unturned alpha cell)

**Date:** 2026-06-25  **Author:** Modeller  **Status:** PRE-REGISTERED before ANY number is produced.
**Code SHA at pre-reg:** `1d46b52` (origin/main HEAD).
**Honesty label:** this is the MARGINAL-BEST remaining alpha idea, NOT a high-conviction bet. Direction is a
10×-settled null; conditioning is the textbook last refuge of overfitting. Every degree of freedom is LOCKED
below BEFORE any run. A clean NO-GO here closes the last alpha door — that is the intended, valuable outcome.

---

## 0. Why this experiment exists (the precise gap)

Every alpha screen on the inventory (SYNTHESIS.md §1) was **UNCONDITIONAL** — a single cross-sectional IC
over the whole panel. An effect that fires ONLY in a vol/liquidity regime (e.g. signed order-flow imbalance
predicts forward DIRECTION only among high-vol, illiquid names where price impact is real, and is averaged
to zero by the calm-liquid majority) is INVISIBLE to every unconditional screen. The 06-20 regime screen
tested a *market scalar → market-aggregate forward* (turbulence→volume); it did NOT test **per-name feature
× regime interactions on a per-name forward return**. That cell is genuinely unturned. This screen tests it,
and only it.

---

## 1. The hypothesis (ONE, falsifiable)

**H1:** there exists at least one pre-committed (feature × regime) interaction whose conditional cross-
sectional rank-IC on the per-name forward return is robustly non-zero IN ONE REGIME BUCKET — i.e. the
feature's predictive power is REGIME-DEPENDENT in a way an unconditional IC washes out — AND the implied L/S
basket clears net-of-(Stage-1-measured)-cost at conservative cuts with per-day NW-t ≥ 2.

**H0 (the prior, expected):** conditional ICs are no larger than the unconditional ICs up to multiple-testing
noise; no (feature × regime) cell survives FDR + the per-day-t + shuffle + lone-outlier gates. ⇒ the
unconditional nulls were not hiding a regime-specific effect; the alpha space is exhausted on current data.

---

## 2. PANEL (locked)

- **Substrate:** the trusted feature store, dense ≥500-sym coverage. Dates = the common trusted window
  **2026-04-15 .. 2026-06-18** (~46 dates; the exact set = the ≥500-sym-coverage dates, fixed at build time,
  logged). Cadence = intraday (per-minute decision instants).
- **Universe per day:** top-200 by trailing ADV (the same liquid tier as the #326/06-25 screens — comparable,
  powered, and the tier where cost is lowest so a real edge has the best chance).
- **Entry discipline:** point-in-time, tradeable **≥ 09:35 ET**, $50k notional floor, no look-ahead (all
  features as-of strictly < decision instant T; regime computed from trailing data < T).
- **Label:** forward per-name return over **H = 15 min** (primary) and **H = 30 min** (secondary), tradeable
  (entry at T+settle ≥ 09:35, exit T+H). Forward returns are the per-name cross-sectional target; demeaned
  per-timestamp (cross-sectional, so market beta is removed by construction).
- **Cost:** Stage-1 measured per-name `realized_half_spread_bps` (round-trip 2× per leg), charged before any
  net-$ claim. NO flat stub for the net claim.

## 3. THE FEATURES (the candidate alpha legs — LOCKED, CLOSED set)

Exactly these **5** trusted, already-shipped, direction-relevant features (NOT a pile; the set is closed, no
adding/dropping post-result). Each is a single per-name score at T:

| # | Feature | Channel |
|---|---------|---------|
| F1 | `ret_15m` (trailing 15-min return) | short-horizon momentum/reversal |
| F2 | `quote_imbalance_15m` | quote pressure (the #326 t=1.93 near-miss — the best unconditional direction candidate) |
| F3 | signed order-flow imbalance (minute-agg, the shipped order-flow group's signed-notional ratio) | trade-flow direction |
| F4 | `ret_60m` (trailing 60-min return) | medium-horizon momentum/reversal |
| F5 | VWAP-deviation (price − session VWAP)/VWAP | mean-reversion anchor |

Rationale for the closed set: these 5 are the direction-relevant features that came CLOSEST to (or are the
canonical carriers of) a directional signal in prior unconditional screens. The hypothesis is that one of
them is direction-predictive ONLY in a specific regime. No GBM/feature-mining leg — this is a targeted
interaction test, not a kitchen-sink fit (a kitchen-sink GBM was already a NO-GO in #326).

## 4. THE REGIMES (the conditioning axis — LOCKED, the fishing surface)

⚠️ **The regime definition IS the fishing surface.** It is fixed here, terciles only, no swept thresholds.

Two regime axes, each split into **TERCILES** (low / mid / high) by the cross-sectional rank of a trailing,
point-in-time per-name scalar at T (so each name is binned by where it sits in that day-minute's cross
section — no look-ahead, no absolute threshold to tune):

- **R-VOL:** trailing realized vol `realized_vol_30m` tercile (low/mid/high vol names).
- **R-LIQ:** trailing dollar-volume / spread liquidity tercile (`book_depth_1m` or trailing $-volume — the
  single shipped liquidity scalar, fixed at build; high tercile = most liquid).

Only the **HIGH and LOW** terciles of each axis are tested as conditioning buckets (the mid tercile is the
washout zone and is not a hypothesis). ⇒ **4 regime buckets** = {R-VOL-high, R-VOL-low, R-LIQ-high,
R-LIQ-low}. No 2-D regime cross (vol×liq) — that is a separate pre-reg if this one fires.

## 5. THE TEST GRID & MULTIPLE-TESTING COUNT (locked)

**Grid = 5 features × 4 regime buckets × 2 horizons = 40 conditional cells.** Plus the 5 features ×
2 horizons = 10 UNCONDITIONAL baseline ICs (the reference — a conditional cell must beat its own
unconditional IC, not just be non-zero).

**The decisive statistic per cell = the INCREMENTAL conditional IC:** conditional rank-IC in the regime
bucket MINUS the same feature's unconditional rank-IC. A cell only counts if the feature is MORE predictive
in that regime than overall (a regime bucket that merely inherits the unconditional IC is not a regime
effect).

**MULTIPLE-TESTING: BY-FDR at q = 0.10 across all N = 40 conditional cells** (Benjamini-Yekutieli, the
dependence-robust form, since the cells share features/panel). No cell is reported as a hit without surviving
BY-FDR across the full 40. This is the entire combos count — no extra regimes, no extra features, no extra
horizons added post-hoc.

## 6. ANTI-FOOLING SPINE (all pre-committed, every prior-cycle gate)

- **Walk-forward, PURGED:** 5 folds over the ~46 dates, purge gap ≥ H between train and test so no forward-
  label leakage across the fold boundary. All ICs/baskets are OOS (test-fold only).
- **PER-DAY NW-t ≥ 2** of the conditional L/S basket return (the cell's high-minus-low decile within the
  regime bucket), net of Stage-1 cost. NOT pooled-$ — the per-day t over the OOS days is the gate (the exact
  discipline that killed the 06-25 tick screen).
- **LONE-OUTLIER TELL:** report the per-day P&L distribution and the max-single-day share; a basket whose $
  is carried by ≤2 days (the tightest-cut artifact) FAILS regardless of pooled $. Report $ at {2%, 5%, 10%}
  cuts and confirm the edge does NOT collapse 3× from 2%→5% (the order-flow signature).
- **SHUFFLE baseline:** within-timestamp label shuffle (permute forward returns within each decision
  instant) — the conditional IC must dominate the shuffled-label conditional IC. Run the shuffle WITHIN the
  regime bucket (so the regime structure is preserved, only the label is permuted) → isolates real
  conditional predictability from regime-induced variance.
- **PREDICT-ZERO baseline:** a no-trade book = $0 (trivial null, confirms the $ columns are real money).
- **DISJOINT-WINDOW replication:** split the OOS dates into two non-overlapping halves; any cell claimed as a
  hit must hold its SIGN (and stay FDR-significant) in BOTH halves. A cell that fires in one half only is
  flagged regime-window-specific and NOT banked.
- **MUST-BEAT-UNCONDITIONAL:** the headline claim is the INCREMENTAL conditional IC (§5), not the raw
  conditional IC — a feature that is equally predictive everywhere is not a regime effect and does not pass.

## 7. GO / NO-GO (FROZEN before any number)

**GO (a regime-conditional edge exists):** at least ONE (feature × regime × horizon) cell satisfies ALL of:
1. incremental conditional rank-IC > 0 AND survives BY-FDR q=0.10 across N=40; AND
2. its net-of-Stage-1-cost L/S basket has per-day NW-t ≥ 2 at a conservative cut (≥5%); AND
3. the $ does NOT collapse ≥3× from the 2%→5% cut (not lone-outlier-driven); AND
4. conditional IC dominates the within-bucket label-shuffle baseline; AND
5. holds SIGN + FDR-significance in BOTH disjoint OOS halves.
→ propose the regime-conditional signal to Lead (a conditional/interaction feature or a regime-gated
strategy), and trigger the §6-equivalent backfill ask ONLY for a disjoint-quarter G5 confirmation.

**NO-GO (expected):** no cell clears all five. → the alpha space is exhausted on current data; the
unconditional nulls were not hiding a regime effect. Route ALL future Modeller EV to (a) delisting-inclusive
universe acquisition (to make weekly-reversal's clean +0.025 IC tradeable) and (b) baseline deployment-
hardening. NO feature PR. NO backfill spend.

## 8. EXECUTION NOTES (for the next cycle that RUNS this)

- Bounded `--rm fp-dev` (or quant-experimenter for the IC fits), `/store` RO, USE_REALIZED_COST per Stage-1,
  `--cpus` capped, NEVER starve live capture. Light-to-moderate compute (46 dates × top-200 × 5 features ×
  40 cells; no GBM, no heavy fit — IC + decile baskets only). Single panel build, cache it.
- NO quantlib/groups edit, NO fingerprint change, NO deploy. experiments/ only.
- Watch the battery fan-out bug (per-group `.unique(["symbol","minute"])` — see #326/#331 notes) when
  building the panel; build directly from contiguous raw/store rows if the battery cartesian-explodes.
- Log the exact date set, the tercile cut values per day, and the per-cell IC table + per-day P&L
  distribution. Emit RESULTS.md with the frozen §7 verdict filled in — blanks only, decision rule fixed here.
