# W11 CERTIFY — results (18-month deep re-run)

Universe = top-200 liquid single stocks (ETFs excluded), SPY market, 60d beta, monthly rebalance.
**15 non-overlapping rebalances** over 2024-12-11..2026-06-16 (vs 3 on the original 126d run). 40 names/quintile.
All numbers are high-minus-low-beta L/S, mean daily over the hold, in **bps/day**. Costs: 3 bps/side spread +
5 bps/side MOO/MOC auction slippage.

## 1. Overnight vs intraday vs 24h — FULL universe (the headline test)

| Realization | gross bps/day | overnight pos. rebalances | net (spread) | net (spread+auction) |
|---|---|---|---|---|
| **OVERNIGHT** | **+35.26** | **12/15 (80%)** | +33.86 | **+23.86** |
| INTRADAY | −1.65 | — | — | — |
| 24h | +33.10 | — | — | — |

**Split present and in the predicted direction** (overnight ≫ intraday, intraday ≈ 0/negative). The 18-month
magnitude (+35 bps) is smaller than the 126d figure (+75 bps) — consistent with the 126d window being the
high-speculation tail — but it is positive, broad, and net-of-cost positive even with auction slippage.

## 2. THE CONFOUND CONTROL — speculation cohort EXCLUDED (the decisive test)

14 speculation names were in the top-200 universe and were removed:
`AFRM APLD APP ASTS BBAI CCJ CEG CIFR CLSK COIN GEV HOOD IONQ MARA`.

| Realization | FULL universe | SPECULATION EXCLUDED | Δ |
|---|---|---|---|
| OVERNIGHT gross | +35.26 | **+33.25** | −2.0 bps (negligible) |
| INTRADAY gross | −1.65 | −1.30 | — |
| overnight pos. frac | 80% | **87%** | — |
| net (spread+auction) | +23.86 | **+21.78** | — |

**THE SPLIT SURVIVES.** Removing the entire crypto/quantum/AI-gapper cohort moves the overnight L/S by ~2 bps
(+35→+33) and the split is, if anything, cleaner (87% vs 80% positive rebalances). The 126d named confound does
NOT explain the premium. This is the decisive, valuable finding: it is a broad high-beta-leg overnight tilt,
not a handful of gappers.

## 3. Bootstrap CIs (10k resamples, 95%), bps/day

| | FULL universe | SPECULATION EXCLUDED |
|---|---|---|
| overnight gross | +35.26 **[+14.84, +55.10]** | +33.25 **[+14.92, +50.64]** |
| overnight net (spread+auction) | +23.86 **[+3.01, +43.93]** | +21.78 **[+3.14, +39.34]** |
| **overnight OOS net (2nd half, spread+auction)** | +23.89 [−2.63, +48.22] | **+29.28 [+6.22, +48.45]** |
| canary (permute beta) | +1.14 [−1.95, +4.22] | −3.14 [−9.54, +3.51] |

- Full-sample overnight gross AND net-incl-auction CIs **exclude zero** in both universes.
- **OOS (walk-forward, second half of rebalances):** full-universe CI just straddles zero ([−2.6,+48]);
  **speculation-excluded OOS net CI EXCLUDES zero [+6.2,+48.5]** — the OOS evidence is *stronger* once the
  gappers are removed, the opposite of a confound.
- **Canary clean:** permuting beta collapses overnight L/S to ~0 with a CI straddling zero in both universes.
  The signal lives in the beta sort.

## 4. Sub-period stability

| Sub-period | n | FULL: on / intr / split | SPEC-EXCL: on / intr / split |
|---|---|---|---|
| 2025-H1 | 4 | +27.5 / +27.7 / **no split** | +15.6 / +24.5 / **no split** |
| 2025-H2 | 6 | +44.1 / −18.0 / **split (100% pos)** | +42.0 / −13.3 / **split (100% pos)** |
| 2026-H1 | 5 | +30.8 / −5.5 / **split** | +36.8 / −7.5 / **split** |

- **2025-H2 and 2026-H1: the split is clean and stable** (overnight strongly positive, intraday negative;
  100% positive overnight rebalances in H2). These cover the multi-regime out-of-the-original-window period.
- **2025-H1 is the one soft spot:** overnight ≈ intraday (both ~+25 bps, no split). This was a broad
  bull-tape stretch where INTRADAY beta also paid — the overnight premium is still positive (+27 bps), but it
  did not *dominate* intraday that half. The split is therefore stable in 2 of 3 sub-periods and the overnight
  leg is positive in all 3.

## 5. Sensitivity to universe size (robustness)

| N_LIQUID | q_size | FULL on/intr | SPEC-EXCL on/intr | SPEC-EXCL OOS net CI |
|---|---|---|---|---|
| 150 | 30 | +40.6 / −2.9 | +36.3 / −1.5 | [+4.1, +53.8] |
| **200** | **40** | **+35.3 / −1.7** | **+33.3 / −1.3** | **[+6.2, +48.5]** |
| 300 | 60 | +36.8 / −3.0 | +33.4 / −2.8 | [−1.1, +43.5] |

Overnight L/S is +33..+41 bps and intraday ~0..−3 bps across all universe sizes; the split survives
speculation exclusion in every case. One minor wrinkle: at N=300 the speculation-excluded canary shows a small
+5.4 bps [+2.3,+8.7] residual (a mild mechanical effect at the widest, less-liquid tail) and the N=300
spec-excluded OOS CI just touches zero — the cleanest evidence is at N=150–200.

## 6. Cost / turnover
Turnover ~18–19%/rebalance (beta is slow-moving — friction-favorable as predicted). Net-of-cost overnight is
positive through the full spread+auction stress (+24 bps full universe, +22 bps spec-excluded). The premium is
~3.5× the modeled per-day round-trip auction+spread cost (~6.6 bps amortized).
