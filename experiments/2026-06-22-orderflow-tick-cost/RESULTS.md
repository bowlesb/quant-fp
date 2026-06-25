# Order-flow / tick-tape G0 screen — RESULTS

**Status: COMPLETE — full-scale run done 2026-06-25. VERDICT: G0a NO-GO (alpha), G0b WEAK (cost), NO backfill
ask.** The full `N_DATES=42 UNIVERSE_TOP=200 N_FOLDS=5` screen ran detached on `fp-dev:latest` (read-only store,
`--cpus 6 --memory 24g`); fc stayed healthy throughout. See "Full-run results" below. The smoke run is retained
for the harness-path record only.

**Code SHA (run):** `822af07c48cf0c172f0635d8230f2d75f9ef5cab` (origin/main, 2026-06-25)
**Code SHA (design):** `d3dac1eb8e4344981345495afb32374293cb7301` (origin/main at design time; run_screen.py unchanged)
**Data-state at design:** raw/trades + raw/quotes broad ~7.6k/4.0k syms/day from 2026-03-30; broad window
2026-03-30..06-18 (~55 trading days). See PRE_REGISTRATION.md §4.

## Smoke verification (2026-06-22, NOT the experiment — 5 days × 80 syms, 3 folds)
Confirms the script runs end-to-end through the real harness and the tick computation is correct:
- Panel built: 373 rows, 5 entry-days, 154 baseline feats + 7 tick proxies, **all proxies + the effective-cost
  label 100% populated** (the trade+quote asof-join + Lee-Ready signing + condition hygiene all work).
- ARM A / ARM B / ARM C all execute; the G0a Δ-curve prints at 2%/5%/10%.
- Realized fwd EFFECTIVE half-spread (5-day smoke): mean 2.32 bps, median 2.05, p10–p90 0.57–4.67 — vs the
  flat stub 3.0 bps and vs G0b-quoted's 7.89 bps mean QUOTED half-spread. **Effective < quoted < — as
  expected** (marketable-limit fills clear inside the displayed spread). This effective-vs-quoted gap is
  exactly the upgrade the cost model targets; confirm it at full scale.
- The 5-day Δ-curve showed the lone-outlier noise pattern (tightest-cut swing on n=8) — meaningless at smoke
  scale; the full 42-day run with per-day t is the real test.

## Full-run results (2026-06-25, the FULL-SCALE run)

**Run config:** `N_DATES=42 UNIVERSE_TOP=200 N_FOLDS=5`, code SHA `822af07c48cf0c172f0635d8230f2d75f9ef5cab`
(origin/main; design-time SHA was `d3dac1eb`, no run_screen.py change since). Detached container
`edgehunt-orderflow-screen` on `fp-dev:latest`, `--cpus 6 --memory 24g`, store = `fp_store_real:ro`.
**Panel:** 4,572 rows, 43 entry-days (2026-04-17..2026-06-18), 436 baseline feats + 7 tick proxies; **all 7
proxies + the effective-cost label 100% populated**. OOS rows per arm = 3,651; 20 OOS entry-days; walk-forward
GBM, 5 folds. Saved panel: `/tmp/edgehunt-out/g0_orderflow_panel.parquet`.

### G0a — ALPHA ($-curve A/B, ARM A vs ARM B)
$-curve from the screen (5-fold). Per-day t and shuffle from `anti_fooling.py` re-run on the saved panel
(the screen prints neither — `run_arm` discards the shuffle/predict-zero curves; t-stat computed on the
per-entry-day net basket return over the 20 OOS days):

| cut | A baseline $ | B +tick-flow $ | Δ total $ | Δ prec | A per-day t | B per-day t |
|----:|----------:|----------:|----------:|-------:|----------:|----------:|
| 2%  | +230,740 | +348,148 | **+117,408** | +0.0000 | **+0.85** | **+1.41** |
| 5%  | +150,471 | +191,061 | **+40,590** | −0.0029 | +1.60 | +1.75 |
| 10% | +104,485 | +129,814 | **+25,329** | +0.0070 | +1.63 | +2.02 |

- **AUC A→B: 0.5284 → 0.5185** (DOWN).  **rank-IC A→B: +0.0382 → +0.0237** (DOWN). Adding the tick proxies
  DEGRADES the cross-sectional ranking metrics.
- **Shuffle baseline** (within-timestamp label shuffle, live−shuffle $): A = +235k/+176k/+140k at 2/5/10%;
  B = +345k/+200k/+146k. Live beats shuffle at all cuts (no gross leakage), but the live edge itself is thin.
- **Predict-zero baseline:** $0 both arms (a no-signal book trades nothing — the trivial null).
- **Per-day t-stats are WEAK and the Δ is NOT robust across cuts.** The Δ$ is +$117k at the 2% cut but
  collapses to +$41k (5%) and +$25k (10%) — the lone-outlier-tightest-cut signature. The 2% cut is n=116
  trades over 20 days (~6/day, a handful of names). No cut clears t≈2 convincingly for ARM A; ARM B only
  reaches t=+2.02 at 10% while its AUC/rank-IC are below baseline. At the tightest cut driving the headline $
  (2%), t is only +0.85 (A) / +1.41 (B) — the big $ there is a few-day artifact, not a robust edge.
- **G0a verdict: NO-GO.** The Δ$ is positive but driven by the noisiest tightest cut, per-day t is weak
  (peaks <2.1, <2 at the dollar-driving cut), and the tick proxies LOWER AUC and rank-IC. This is the prior
  0/4 OFI null reproduced on the finer tick substrate: gross ranking does not improve and the $-gain is
  not per-day-robust. Matches the pre-reg's predicted null.

### G0b — EFFECTIVE-COST model (the deliverable)
- OOS R² = **+0.122**  rank-IC = **+0.453**  MAE: model **1.47 bps** vs flat-stub **1.59 bps**
  (**+7% error reduction**)  folds = **3** (only 3 of the 5 fold-windows met the cost-model's internal
  600-train/100-test row floor).
- Realized fwd effective half-spread: **mean 2.95 bps, median 2.41, p10 0.82, p90 5.60** (flat stub = 3.0 bps).
  At full scale the effective half-spread mean (2.95) is essentially AT the flat 3.0-bps stub — the
  5-day-smoke "2.32 bps, effective << quoted" gap did NOT hold up at 42-day scale.
- **G2-incremental (effective vs the already-wired QUOTED model):** NOT computed — the screen does not emit the
  quoted-spread label column (it prints the NOTE that the quoted label must be added with the same
  `realized_half_spread_bps_multi` the panel uses before any wire). So the decisive "effective beats quoted"
  comparison is unmeasured; only effective-vs-flat-stub is available, and that is only +7%.
- **Effective-cost verdict: WEAK — not a keeper this run.** R²=0.122 is below the screen's own 0.2 keeper bar,
  MAE reduction over the flat stub is only +7% (vs the quoted G0b's R²=0.575 / 18% haircut), the realized mean
  sits at the 3.0-bps stub, and the effective-vs-quoted incremental (the actual wire-decision gate) is not
  even measured. No basis to upgrade `_attach_realized_half_spread` on this evidence.

## Net read
- **Alpha (G0a): NO-GO, clean.** Tick-level Lee-Ready signed-notional/block/persistence proxies add no robust
  net-$ over the full trusted + minute-agg order-flow + quote baseline. The +$ Δ is concentrated in the
  noisiest 2% cut (collapses 3x→5%), per-day t is weak (<2 at the $-driving cut), and the proxies LOWER AUC
  and rank-IC. The anti-fooling discipline (per-day t + shuffle) is exactly what kills it — a +$ total alone
  would have looked like a win. This reproduces the settled order-flow 0/4 null on the finer substrate.
- **Cost (G0b): WEAK, not a keeper this run.** Effective-spread is well-RANKED (rank-IC 0.453) but poorly
  LEVELED (R²=0.122, +7% MAE over a flat stub), the realized mean sits at the 3.0-bps stub (no material gap to
  exploit at scale), and the effective-vs-quoted incremental — the only comparison that justifies a wire — is
  unmeasured because the quoted label column isn't emitted.
- **Backfill ask: NO.** The §6 one-quarter breadth-back extension is conditional on a G0 GO (alpha) or a
  cost-keeper wanting cross-regime robustness. Neither fired. Do NOT spend Ben's budget on the backfill on this
  evidence. If anyone wants to resurrect the cost angle, the cheap next step is FREE on the current window: add
  the quoted-spread label to the panel and run the effective-vs-quoted G2-incremental — only if that shows
  effective materially better-predicted than quoted does the backfill (or a wire) become worth discussing.

## Caveats / honesty
- Only 20 OOS entry-days (5-fold walk-forward over 43 days) — a single ~2-month regime. Even a real thin edge
  could not be confirmed here without G5's disjoint quarter; but the point is the edge is NOT thin-but-real,
  it is absent-on-the-robust-metrics (AUC/rank-IC down, per-day t weak).
- The cost model ran only 3 effective folds (row-floor), so its R² is on limited OOS — but the direction
  (poorly leveled, mean at stub) is consistent and the quoted comparison is the missing piece regardless.
- The slight AUC/rank-IC difference between the screen (A: 0.5284) and the anti-fooling re-run (A: 0.5265) is a
  benign column-cast difference in panel assembly; the verdict (B below A on both metrics) is identical in both.
