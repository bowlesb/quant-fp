# Order-flow / tick-tape G0 screen — RESULTS

**Status: PLACEHOLDER — the screen has NOT been run at full scale.** This experiment is the design + the
one-call-ready runnable (`run_screen.py`, README.md). Fill this in after the Lead greenlights and the full
`N_DATES=42 UNIVERSE_TOP=200` run completes. The smoke run below proves the harness path, not the verdict.

**Code SHA:** `d3dac1eb8e4344981345495afb32374293cb7301` (origin/main at design time)
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

## Full-run results — TO FILL IN
### G0a — ALPHA ($-curve A/B, ARM A vs ARM B)
| cut | A baseline $ | B +tick-flow $ | Δ total $ | Δ prec | per-day t |
|----:|----------:|----------:|----------:|-------:|----------:|
| 2%  |           |           |           |        |           |
| 5%  |           |           |           |        |           |
| 10% |           |           |           |        |           |
- AUC A→B: …  rank-IC A→B: …  shuffle (both arms): …  predict-zero: …
- **G0a verdict:** GO / NO-GO — …

### G0b — EFFECTIVE-COST model (the deliverable)
- OOS R² = …  rank-IC = …  MAE: model … bps vs flat-stub … bps (… % reduction)  folds = …
- Realized fwd effective half-spread: mean … median … p10–p90 …
- **G2-incremental (effective vs the already-wired QUOTED model):** predicted-effective MAE … vs
  predicted-quoted MAE … on the SAME OOS folds → effective is/ isn't materially better-predicted.
- **Effective-cost verdict:** KEEPER (upgrade `_attach_realized_half_spread`) / weak — …

## Net read — TO FILL IN
- Alpha: …
- Cost: …
- Backfill ask (only if G0a GO or cost-robustness wanted): the §6 one-quarter breadth-back extension.
