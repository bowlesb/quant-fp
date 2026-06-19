# Lane C — overnight (close→next-open) directional edge: RESULTS

Pre-registration: `hypothesis.md` (HEAD `5d280dd`, branch `main`). Gate (1d headline, fixed before
results): REAL OOS rank-IC ≥ 0.01 AND (REAL − SHUFFLE) ≥ 0.01 AND NW|t| ≥ 2.0 AND
breakeven_cost_bps > 10.0. 2d/3d are descriptive by-horizon, not additional shots at the headline.

## Data-integrity finding (caught BEFORE any verdict — load-bearing)

The first smoke run produced a fantastical result (label_std ≈ 0.77, 1d L/S Sharpe ≈ 10,
breakeven ≈ 295 bps, max raw overnight return = **145×**). Diagnosis: the 15:59-ET close used as the
overnight ENTRY price is, for sub-$1 / sub-penny names, frequently a **bad/odd-lot print** — e.g.
JTAI 2026-04-07 `rth_close=$0.0388` → next-day `exec_0935` ≈ $8.8 → raw overnight +226×. These
corrupt prints dominated the LightGBM fit and the L/S P&L (rank-IC less so, but the economic gate
entirely). They are NOT signal; they are tape garbage on illiquid names.

Methodology hardening (documented in `build_overnight_dataset.py`, applied SYMMETRICALLY so it cannot
inject directional signal):
- **MIN_PRICE = $1** floor on BOTH legs (entry close_d and exit exec_{d+k}) AND on the entry row's X
  (the standard penny-stock exclusion; sub-$1 names aren't realistically tradeable overnight anyway).
- **Per-day winsorization** of the RAW overnight return at [0.5%, 99.5%] before the cross-sectional
  median (kills residual bad-print tails; symmetric, pre-excess).

Effect: 1d overnight excess-return std collapses 0.77 → **0.0267** (realistic), max 145× → **0.23**.
ALL downstream numbers below are on the cleaned panel. This is exactly the silent-garbage class the
discipline exists to catch — a researcher who skipped the label-distribution check would have
"found" a spectacular fake overnight edge.

## SMOKE result (most-recent 60 calendar days → 38 labelled OOS days, ~117k rows, ~5300 symbols)

Disciplined harness (`run_overnight_baseline.py` → `quantlib.research.run_experiment`):
walk-forward purge (horizon_minutes=1440), within-day shuffle canary, NW-t (lag=3 conservative),
net-of-cost L/S, cadence_min=390 → periods_per_year=252.

| horizon | rows | OOS days | REAL IC | SHUFFLE IC | REAL−SHUF | NW t | net/period | Sharpe | breakeven bps | turnover |
|---------|------|----------|---------|------------|-----------|------|-----------|--------|---------------|----------|
| **1d**  | 116946 | 38 | **0.01805** | −0.00008 | **+0.01813** | **0.843** | 0.001789 | 3.81 | **8.31** | 2.86 |
| 2d      | 113625 | 37 | 0.00755 | 0.01877 | −0.01122 | 0.418 | 0.00415 | 5.56 | 16.57 | 2.86 |
| 3d      | 110430 | 36 | 0.00188 | 0.00350 | −0.00162 | 0.081 | 0.00358 | 3.76 | 15.47 | 2.67 |

**1d GATE: MISS / null.** IC ≥ 0.01 ✓ and edge-vs-shuffle ≥ 0.01 ✓, but **NW |t| = 0.84 ✗** (the IC
is statistically indistinguishable from zero across 38 days) and **breakeven = 8.31 bps ✗** (below the
10 bps realistic-overnight-cost bar). 2d/3d do not even clear the IC/edge legs (negative vs shuffle).

Top features by gain are now diffuse (overnight_prev, intraday_prev, ret_10d, ret_20d, ret_1d…) —
no single dominator, consistent with a weak, broadly-distributed effect rather than a strong edge.

The smoke validated the full pipeline end-to-end and caught the data-integrity bug. Its main
limitation is POWER: 38 OOS days cannot resolve a |t| ≥ 2.0 on an IC of ~0.018. The pre-registered
rationale for the 18-month panel is exactly this power gap.

## FULL 18-month panel (2024-12-11 → 2026-06-18, 357 OOS days, ~6075 symbols, 693,640 rows)

Powered test — ~10× the smoke's OOS days. Same gate, same cleaning, same harness.

| horizon | rows | OOS days | REAL IC | SHUF IC | REAL−SHUF | NW t | net/period | Sharpe | breakeven bps | turnover |
|---------|------|----------|---------|---------|-----------|------|-----------|--------|---------------|----------|
| **1d**  | 693640 | 357 | **0.03539** | 0.00115 | **+0.03424** | **3.887** | 0.005961 | 4.92 | **21.95** | 3.00 |
| 2d      | 689346 | 356 | 0.02303 | 0.00110 | +0.02193 | 2.364 | 0.010427 | 5.53 | 37.50 | 2.94 |
| 3d      | 686271 | 355 | 0.01132 | −0.00448 | +0.01580 | 1.000 | 0.009012 | 4.25 | 34.63 | 2.77 |

**1d GATE: all four legs PASS → HIT** (IC 0.035 ≥ 0.01 ✓, edge +0.034 ≥ 0.01 ✓, NW t 3.89 ≥ 2.0 ✓,
breakeven 21.95 > 10 ✓). The by-horizon t-stat decays monotonically 3.89 → 2.36 → 1.00, consistent
with a real overnight effect that decays over the following sessions (not a spurious spike).

BUT the top feature by gain is **`dollar_vol_20d`** (liquidity/size) — the model is substantially
ranking on illiquidity. That triggers the decisive tradeability robustness test.

## ROBUSTNESS — LIQUID universe (top-1500 by 20d ADV, 356 OOS days, 1486 symbols, 323,876 rows)

Same everything, restricted to the genuinely tradeable, low-cost names (where the 22 bps full-universe
breakeven would actually be realizable at ~5-10 bps round-trip):

| horizon | rows | OOS days | REAL IC | SHUF IC | REAL−SHUF | NW t | Sharpe | breakeven bps |
|---------|------|----------|---------|---------|-----------|------|--------|---------------|
| **1d**  | 323876 | 356 | 0.01098 | 0.00372 | **+0.00726** | **1.20** | **0.885** | **4.12** |

**1d GATE on liquid universe: MISS** (edge +0.0073 < 0.01 ✗, t 1.20 < 2.0 ✗, breakeven 4.12 < 10 ✗).
The edge COLLAPSES: t 3.89 → 1.20, Sharpe 4.92 → 0.89, breakeven 21.95 → 4.12 bps. And 4.12 bps
one-way does NOT cover even liquid-name overnight round-trip (MOC + next-open auction ≈ 5-10 bps).

## VERDICT — NUANCED NULL for the tradeable cut

The cross-sectional overnight (close→next-open) directional effect is **statistically real but
economically fragile and NON-tradeable**:
- On the FULL universe it clears every pre-registered gate leg (t=3.9, breakeven 22 bps) — a genuine,
  shuffle-clean, well-powered effect, NOT noise.
- But it lives ENTIRELY in the illiquid / small-cap tail (rank 1500-6075). `dollar_vol_20d` is the
  dominant feature; restricting to the top-1500 tradeable names kills it (t=1.2, edge < shuffle gate,
  breakeven 4 bps). Small-cap overnight round-trip costs (MOC + next-open auction spreads on rank
  1500-6000 names) routinely EXCEED the 22 bps full-universe breakeven, so even the full-universe
  gross IC does not robustly survive REALISTIC small-cap costs — the pre-registered 10 bps bar was,
  if anything, too lenient for that cohort.

So: the intraday cross-sectional DIRECTION null does NOT simply extend to overnight — there IS a real
overnight cross-sectional direction signal — but it is an **illiquidity-concentrated, cost-dominated
microcap effect**, not a tradeable liquid-universe alpha. The honest call for a deployable directional
strategy is **NULL**; the honest call for "is the overnight regime different from intraday" is **YES,
weakly, but only where you can't trade it cheaply**.

## STRATEGY IMPLICATION / NEXT STEP

- DO NOT promote this to a directional L/S strategy on the tradeable universe — it fails there.
- The real, shuffle-clean full-universe signal is a documented next lead IF a low-cost small-cap
  overnight execution path exists (it likely does not at our size on rank 1500-6000 names; route the
  cost question to Lead/execution before any further work).
- The result HARDENS a related prior: our features rank illiquidity/size strongly. The overnight HIT
  is the SAME diagnostic the intraday nulls flagged (features carry liquidity, not clean direction),
  now showing through as a microcap overnight-premium artifact rather than tradeable alpha.
- Pre-registered multiplicity discipline honored: 1d is the single headline; 2d/3d reported as the
  descriptive by-horizon table, NOT promoted. No threshold relaxation after seeing numbers.

## STATUS

Lane C cycle COMPLETE. Headline = NUANCED NULL: real overnight cross-sectional direction signal
(full-universe t=3.9, shuffle-clean) that is illiquidity-concentrated and NON-tradeable in the liquid
universe (t=1.2, breakeven 4 bps). Data-integrity bug (sub-$1 bad-print 145× returns) caught and
fixed before any verdict. All scratch in experiments/; no quantlib/live/fingerprint edit.
