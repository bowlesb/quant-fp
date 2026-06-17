# W11 FULL300 CERTIFICATION — wider-universe confirmation (top-300, ~60/quintile)

## VERDICT: **CERTIFIED-KEEP — strengthened vs the 168-name run.** The overnight-beta premium holds at full power, the confound control survives, AND the wider cross-section TIGHTENS the auction-stressed OOS net CI to EXCLUDE zero in both universes.

This is the wider-universe confirmation of the preliminary 168-name certify (see `certify.md` /
`certify_results.json`, ~33 names/quintile). The entire top-300-by-dollar-volume is now at 378d
(18 months, 2024-12-11..2026-06-16), so beta quintiles are ~60 names/leg — nearly double the
cross-sectional power. Identical construction (60d rolling SPY beta, monthly beta-quintile
high-minus-low overnight/intraday/24h L/S, speculation-cohort exclusion, sub-period split, spread +
MOO/MOC auction stress, per-rebalance bootstrap, walk-forward OOS, beta-permute canary). Source:
`certify300_daily.parquet` (5223 deep symbols → top-300 liquid single stocks; SPY = market; ETFs excluded).

### Setup at full power
- **300-name universe → q_size = 60 names/quintile** (vs ~33 in the 168 run), **15 non-overlapping monthly rebalances**, avg turnover ~17–20%.
- The top-300-by-dollar-volume now contains **27 speculation-cohort names** (vs 14 in the narrower run):
  AFRM, APLD, APP, ASTS, BBAI, CCJ, CEG, CIFR, CLSK, COIN, GEV, HOOD, IONQ, MARA, MSTR, NRG, OKLO,
  PLTR, QBTS, QUBT, RGTI, RIOT, RKLB, SMCI, SOFI, VST, WULF. This makes the confound control STRONGER
  (almost 2× as many gappers to remove).

### Evidence

1. **The split holds — and is larger at full power.** Overnight high-minus-low-beta L/S =
   **+41.6 bps/day** (87% of 15 rebalances positive) vs intraday **−3.9 bps/day**. Full-sample
   bootstrap CI **[+19.6, +63.6] excludes zero**. (168-run was +35 / −1.7, [+14.8, +55.1].) The
   wider, deeper cross-section reads STRONGER, not weaker.

2. **It SURVIVES the confound control at 2× the gapper count — the decisive durability test.**
   Removing all 27 speculation-cohort names moves the overnight L/S by only ~5.5 bps
   (+41.6 → **+36.0**), 87% positive, full-sample CI **[+17.9, +53.5] excludes zero**. Removing nearly
   double the gappers of the 168-run barely dents the edge. **It is a broad high-beta-leg overnight
   tilt, NOT the crypto/quantum/AI gap regime.**

3. **THE KEY QUESTION RESOLVED — the auction-stressed OOS net CI now EXCLUDES zero in BOTH universes.**
   With 3 bps/side spread + 5 bps/side MOO/MOC auction slippage, the walk-forward OOS (second half of
   rebalances) overnight net-incl-auction bootstrap CI is:
   - **FULL universe: +28.4 bps, [+1.2, +53.3] — excludes zero.**
   - **SPEC-EXCLUDED: +30.5 bps, [+7.9, +48.9] — excludes zero (cleaner).**
   In the 168-run the FULL OOS net-incl-auction straddled zero ([−2.6, +48]) and only the
   spec-excluded version excluded it. **The wider cross-section TIGHTENED the CI and lifted the full
   universe off zero** — this is the read the task hoped the added power might resolve, and it did.
   Full-sample overnight net-incl-auction: FULL **+30.2** [+8.1, +52.5], SPEC-EXCL **+24.5** [+6.2, +42.2].

4. **Canary clean.** Permuting beta→name collapses overnight L/S to ~+3 bps with the CI essentially at
   zero in both universes (FULL [−1.5, +8.2], SPEC-EXCL [−2.0, +7.6]) — the signal is the beta sort,
   not a degenerate cross-section.

### Honest caveats (still CERTIFIED-KEEP, not a finished strategy)

- **2025-H1 remains regime-conditional.** In the spec-EXCLUDED universe, 2025-H1 still shows **no split**
  (overnight +22.6 = intraday +22.6, split_ok=False) — in that broad bull-tape half intraday beta paid
  too. The split is clean and strong in 2025-H2 (on +42 / intr −13, 100% positive) and 2026-H1
  (on +40 / intr −9, 80% positive). In the FULL universe 2025-H1 nominally passes (on +37 > intr +27)
  but the overnight edge over intraday is thin there. **The premium is regime-conditional to a degree —
  this is the same residual risk the 168-run flagged, and the added power does NOT remove it.**
- **Auction fills are modeled, not measured.** The 5 bps MOO/MOC slippage is a conservative stress, not
  real auction-fill data. A paper container MUST measure actual MOO/MOC execution before real capital —
  the OOS net excludes zero by ~1 bp at the FULL-universe lower bound, so realized auction cost is the
  binding uncertainty.
- **n=15 rebalances** is real power but still modest; 2025-H1 is 4 of those 15.

### Recommendation
**Promote to a paper-tradeable lead with HIGHER confidence than the 168-run warranted.** Low-turnover,
beta-quintile, OVERNIGHT-only high-minus-low L/S in the top ~200–300 liquid single stocks, monthly
rebalance, executed at close/open auctions. Next steps before real capital: (1) a paper container that
measures *actual* MOO/MOC auction slippage and confirms the ~+25–30 bps net survives real fills
(this is now the single binding uncertainty); (2) monitor regime-conditionality (the 2025-H1 no-split
half) — consider a tape / intraday-beta filter; (3) extend depth as history accrues for more rebalances.

**One-line:** At full power (top-300, ~60/quintile, 27 gappers in-universe) the overnight beta premium
is **STRONGER and CLEANER** than the 168-run: +41.6 (FULL) / +36.0 (spec-excl) bps overnight vs ~−4
intraday, survives removing ~2× the speculation cohort, and — decisively — the auction-stressed
walk-forward OOS net CI now **EXCLUDES zero in BOTH universes** ([+1.2,+53.3] FULL, [+7.9,+48.9]
spec-excl). The wider universe resolved the 168-run's main ambiguity in the edge's favor.
**CERTIFIED-KEEP — strengthened.**
