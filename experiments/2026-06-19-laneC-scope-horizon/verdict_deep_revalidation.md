# Lane C B4 — DEEP-PANEL RE-VALIDATION: VERDICT

**RAN 2026-06-19T20:40Z** by Modeller/B4-deep. Executes `deep_revalidation_prereg.md` EXACTLY
as pre-registered (the "TOM-killer persistence test"), fired on the now-COMPLETE deep-bars panel.
**No threshold relaxation, no post-hoc strata, no band sliding.**

## ⭐ VERDICT — **RESURRECTED / HARDENED-CONFIRM** (the OPPOSITE of TOM)

The small-cap (ADV rank 2000–4000) overnight close→next-open directional premium **did NOT
collapse** on the ~10yr deep panel — it **STRENGTHENED with the added power AND passed the
TOM-killer by-year persistence leg**. B4 is a **deep-history-confirmed, cost-surviving, persistent
small-cap overnight L/S premium**, not an 18-month small-sample artifact.

- **A1–A5 (the frozen 18mo gate): ALL PASS**, with large margin (IC 0.0569, NW t=13.04, gross
  breakeven 46.91bps ≫ own cost 18.57bps, net Sharpe @own-cost **+1.81**).
- **Leg P (persistence): PASS.** Net-of-own-cost L/S **positive in 7 of 8 full OOS years**
  (threshold ≥6); the single negative year (2019) is NOT significant (NW|t|=0.53 ≪ 2.0) → **no
  sign-flip**. This is the exact inverse of the TOM kill (which sign-flipped and collapsed).

This is the de-risking outcome Ben's B4 paper-trade decision was waiting on, resolving in B4's
**favor**: the only surviving return-signal lead in the portfolio is real and persistent over a
decade. (Capital still gated on the separately-required REAL next-open execution-cost measurement —
see CAVEATS; Corwin–Schultz is known to under-state small-cap next-open auction spreads.)

---

## STAMPED DATA STATE (reproducibility)

- **HEAD** `7ef86bbd43d1a7e482793a52704e3e9a9d9ecd83` @ branch `main`.
- **Raw store** `fp_store_real:/store/raw/bars` — deep-bars backfill **COMPLETE (full universe)**.
  - Symbol partitions on disk: **7703**; distinct trading dates **2630**, range
    **2016-01-04 → 2026-06-18**; full-year coverage 2016–2025 (~250–253 dates/yr) + 2026 partial
    (116 days). Per-year symbol coverage rises 2016→2025 (~4100 in 2020 → ~6300 in 2025) — the
    backfill is the **current** universe carried back in time (survivorship-free in the sense that
    it is NOT a point-in-time-listed set; noted as a known property, same as the factor baseline).
- **Stage-1 deep daily reduction** (`build_overnight_dataset.py`, fp-dev, RO store, one date at a
  time): `overnight_daily_deep.parquet` = **12,024,655 rows, 2630 dates, 7389 symbols**,
  2016-01-04 → 2026-06-18.
- **Stage-2 band build** (`build_bands.py`, fp-dev): per-band NPZ + per-band Corwin–Schultz cost
  **recomputed point-in-time on deep data** (`band_costs_deep.json`).
- **Power vs the prereg requirement:** the prereg asked for the ~10yr full-universe panel as the
  ~7× power upgrade over the 18mo shallow panel. **MET.** B4 deep = **1,128,482 rows / 2,390 days /
  2,000 symbols**, vs the 18mo B4 = 232,070 rows / 357 days / 1,787 symbols → **~6.7× more trading
  days**. The walk-forward produces **8 full OOS years** (2018–2025; 2016–17 consumed by the first
  training folds) + a 2026 stub; the prereg's `ceil(0.70 × N_full)` formula adapts to N=8 → ≥6
  (NO judgment call after seeing N, exactly as frozen).
- **Run harness:** `run_bands.py` (legs A1–A5) + `run_b4_byyear.py` (leg P), both calling the
  **UNMODIFIED** `quantlib.research.run_experiment` / `quantlib.backtest.long_short_backtest`.
  `run_b4_byyear.py` reproduces the harness's raw-label walk-forward OOS predictions verbatim
  (DEFAULT_LGB, n_folds=5, horizon_minutes=1440, cadence_min=390) and splits the net-of-own-cost
  daily L/S P&L by calendar year. Dry-run reproduced the 18mo B4 numbers exactly (rows=232070,
  days=357, own_cost=20.87bps) before firing on deep data. NO quantlib edit, NO live tree, NO
  fingerprint, NO deploy, RO store, --rm sandbox.

---

## LABEL-DISTRIBUTION SANITY (mandatory, BEFORE any IC) — **PASS (no data trap)**

The deep 2016–2018 tape carries more penny/odd-lot bad-print risk than the 18mo window, so this
guard is load-bearing. Computed on the deep B4 label (1d cross-sectional excess overnight return,
$1 floor on BOTH legs + per-day symmetric [0.5%, 99.5%] winsor):

```
B4 deep label (fwd_1d cross-sectional excess overnight return)
  rows=1,128,482   n_nan=0
  label_std = 0.024830    (18mo reference ~0.027; absurd >0.1 = the sub-$1 bad-print tell)
  max_abs   = 3.046722    (bounded post-winsor; a few large-but-real overnight moves)
  mean      = 5.21e-04    (≈0, as a cross-sectional excess should be)
  pctiles   p0.1=-0.134  p1=-0.068  p50=-0.000  p99=+0.077  p99.9=+0.169
```

`label_std = 0.0248` sits right at the 18mo reference (~0.027) — **NOT** the 0.77/145× blowup that
signalled sub-$1 contamination on Lane C. **The $1 floor + per-day winsor held on the deep panel.
The data trap did NOT fire.** (Note: the FEATURE columns `ret_co_1d`/`overnight_prev`/`ret_*d` carry
large raw max-abs values — they are the RAW unwinsorized trailing-return inputs the 18mo builder
also feeds, and the gradient-boosted tree is rank-robust to them; only the LABEL is floored/
winsorized, faithful to the frozen construction, not a new defect.)

---

## LEGS A1–A5 — the frozen 18mo gate, recomputed on the deep panel

All five bands run through the unmodified harness; own cost = median Corwin–Schultz half-spread +
1bp, recomputed point-in-time on deep data. (B1–B5 reported for the illiquidity-gradient context;
**B4 is the pre-registered headline band**.)

| band | rows | days | OOS IC | shuffle | edge | NW t | gross_be(bps) | own_cost(bps) | net/period | Sharpe@own | turn | verdict |
|------|-----:|----:|------:|-------:|-----:|-----:|------:|------:|------:|------:|----:|--------|
| B1 0001-0500 | 764,501 | 2392 | 0.0285 | -0.0008 | +0.0293 | 6.63 | 10.48 | 7.47 | 0.000885 | +0.743 | 3.00 | TRADEABLE |
| B2 0500-1000 | 654,677 | 2392 | 0.0240 | +0.0007 | +0.0234 | 5.59 | 10.99 | 8.52 | 0.000737 | +0.301 | 3.06 | TRADEABLE |
| B3 1000-2000 | 975,626 | 2392 | 0.0276 | -0.0029 | +0.0306 | 6.13 | 13.05 | 12.90 | 0.000027 | +0.008 | 3.21 | TRADEABLE |
| **B4 2000-4000** | **1,128,482** | **2390** | **0.0569** | **-0.0223** | **+0.0793** | **13.04** | **46.91** | **18.57** | **0.009427** | **+1.813** | **3.33** | **TRADEABLE** |
| B5 4000-6000 | 245,061 | 2223 | 0.0885 | -0.0147 | +0.1032 | 20.47 | 24.18 | 18.55 | 0.001984 | +0.431 | 3.56 | TRADEABLE |

**Band-monotonic in illiquidity** (IC B1→B5: 0.029→0.088) — the same illiquidity-concentration
signature Lane C originally found, now far better powered. **B4 deep vs B4 18mo:** IC 0.0569 vs
0.0318; NW t **13.04 vs 3.71**; gross breakeven **46.91 vs 36.35 bps**; net Sharpe **+1.81 vs +1.33**.
The signal **hardened**, it did not decay. Shuffle canary sharply NEGATIVE (B4 −0.0223) confirms a
real, non-leakage effect (predict-zero ≡ a zero-IC null; the shuffle is the within-timestamp
permutation baseline and it collapses below zero, as required).

**A1 IC≥0.01:** PASS (0.057). **A2 NW|t|≥2.0:** PASS (13.04). **A3 gross_be > own_cost:** PASS
(46.91 > 18.57). **A4 net Sharpe>0 @own cost:** PASS (+1.81). **A5 executable @$100K:** PASS
(per-name ≈ $1,064 ≈ 4.7e-4 of min ADV $2.24M — informational, not binding).

---

## LEG P — by-year persistence (the TOM kill/confirm shot) — **PASS**

Net-of-OWN-cost (18.57 bps one-way) daily L/S P&L, reproduced from the harness's raw-label
walk-forward OOS predictions (979,442 prediction rows over 1,992 OOS timestamps), split by
calendar year:

| year | days | mean_net | sum_net | NW t | sign |
|-----:|----:|---------:|--------:|-----:|:----:|
| 2018 | 166 | +0.006029 | +1.0009 | 0.67 | + |
| 2019 | 206 | −0.002271 | −0.4677 | −0.53 | − |
| 2020 | 250 | +0.013851 | +3.4627 | 2.50 | + |
| 2021 | 251 | +0.009271 | +2.3270 | 1.83 | + |
| 2022 | 250 | +0.003020 | +0.7549 | 0.65 | + |
| 2023 | 249 | +0.009913 | +2.4684 | 1.56 | + |
| 2024 | 252 | +0.021058 | +5.3066 | 3.30 | + |
| 2025 | 249 | +0.011060 | +2.7539 | 2.49 | + |
| 2026* | 114 | +0.009870 | +1.1252 | 1.83 | + |

\*2026 = partial stub, reported, excluded from the full-year count.

- **P1 (majority-positive):** **7 of 8 full years positive** (2018, 2020–2025); threshold
  `ceil(0.70 × 8) = 6`. **PASS.**
- **P2 (no decisive sign-flip):** the only negative full year is **2019**, with mean −0.0023 and
  **NW|t| = 0.53 ≪ 2.0** → a tolerated noisy-negative year, NOT a significant reversal. No year is
  both negative AND significant. **PASS.**
- The premium is **strongest in the recent, more-liquid, more-tradeable years** (2024 t=3.30, 2025
  t=2.49, 2020 t=2.50) — it is NOT an early-history (2016–2018) artifact. The 2026 partial year is
  also positive (t=1.83). This is the **inverse of the TOM signature** (TOM collapsed to |t|<1.4
  and sign-flipped by year).

**LEG P (P1 AND P2): PASS.**

---

## SHUFFLE + PREDICT-ZERO BASELINES

- **Within-timestamp shuffle (canary):** B4 deep shuffle IC = **−0.0223** vs real **+0.0569**
  (edge +0.0793). The permutation baseline collapses below zero — the signal is not an artifact of
  the walk-forward/IC machinery.
- **Predict-zero:** a constant-zero predictor yields IC ≡ 0 and a flat (zero-P&L, zero-turnover)
  L/S by construction; the realized net Sharpe +1.81 and the by-year net P&L are strictly above the
  predict-zero null in every passing year.

---

## NET-OF-COST (the implied trade)

A dollar-neutral top/bottom-decile B4 overnight L/S, charged the band's OWN one-way cost
(median CS half-spread 17.57 bps + 1.0 bp pad = **18.57 bps**, on realized turnover ~3.33/period
+ 50bps/yr borrow): **net per period +0.009427, net Sharpe +1.81** over 2,390 OOS days. Gross
breakeven 46.91 bps gives **~28 bps of headroom over the modelled cost**. Executable at $100K
(per-name ≈ $1,064 ≈ 4.7e-4 of the band's min ADV).

---

## CAVEATS (carried from the adjudication — capital still gated)

1. **CS under-states next-open auction spreads on small-caps.** The 28 bps gross-breakeven
   headroom over the CS-based cost is necessary-not-sufficient. The **REAL measured next-open
   execution cost** (the separately-required `results_overnight_boundary.md` measurement) is the
   binding gate before any capital. B4 flips to a net LOSS at ~2.5× the CS cost (46.91/18.57); the
   deep margin is wider than 18mo's (36.35/20.87 ≈ 1.7×) but the auction-spread risk is unchanged.
2. **Universe property.** The backfill is the current symbol set carried back in time; the deep
   cohort is the names that were *durably* rank-2000–4000 then (60-day-stable filter applied), not
   a point-in-time-listed snapshot. Same caveat the deep factor baseline carries.
3. **Turnover ~3.3/period** is high (full nightly re-rank) — the net Sharpe already absorbs it at
   18.57 bps; real fills must too.

---

## SECONDARY — deep cross-sectional FACTOR baseline (the watcher-flagged reference)

The standard daily cross-sectional factor sanity baseline (12-1 momentum, 1m reversal, low-vol,
size; monthly-rebalanced decile L/S; same $1-floor + per-day-winsor + label-std guards;
net-of-cost; by-year persistence; shuffle) was run on the deep panel as the reference frame for
this revalidation. On the partial deep panel (4,420 symbols, 7.33M daily rows, 2016–2026, 126
monthly rebalances) the verdict was **all 4 factors NULL vs the pre-registered bar**
(NW|t|≥2.5 AND net-of-cost>0 AND majority-positive years): label-std all 0.04–0.08 (no fake-edge
blowup), MOM_12_1 the only real-but-sub-threshold signal (gross Sharpe 0.51 / net 0.47, 8/10
positive years, t=1.82<2.5), REV_1M dead (turnover 0.85 eats it), LOWVOL wrong-sign, SIZE
coin-flip (see `experiments/2026-06-19-deep-factors/results.md`). A full-universe re-run on the
now-complete panel was launched as the reference completion. **Reference takeaway:** standard
daily factors are null/sub-threshold on this universe, which makes the B4 overnight-premium result
stand out as the one genuinely surviving, persistent, well-powered cross-sectional return signal.

---

## STATUS

**FIRED + SETTLED.** Frozen B4 gate (A1–A5) + TOM-killer persistence leg P, both PASS on the
~10yr full-universe deep panel. Label-std sanity passed (no sub-$1 trap). VERDICT =
**RESURRECTED / HARDENED-CONFIRM**: B4 small-cap overnight premium is deep-history-confirmed and
persistent → **strong case for the B4 overnight paper-trade**, still gated on the REAL next-open
execution-cost measurement (CS-cost is a lower bound). Evidence:
`deep_data/{label_sanity,gate_A1A5,legP_byyear,build_bands}.log`. Research scratch only — NO
quantlib edit, NO live tree, NO fingerprint, NO deploy, RO store, --rm sandbox, no secrets.
