# Experiment Log

Append-only history of all experiments (the Modeller's exploration). IC is vs the actual forward return; the shuffle canary is the leakage arbiter. Thin panel -> exploration, not edge.

| run_at | id | horizon | label | feats | rows | mean_IC | NW_t | canary | hypothesis |
|---|---|---|---|---|---|---|---|---|---|
| 2026-06-10T21:57:30+00:00 | E0_raw_18 | fwd_30m | raw | 18 | 570481 | 0.02052 | 2.976 | -0.00107 | Baseline: regression on raw fwd_30m excess, all 18 features. Reproduces the trainer result; reference point. |
| 2026-06-10T21:57:47+00:00 | E0p_rank_18 | fwd_30m | rank | 18 | 570481 | 0.00965 | 1.373 | 0.00504 | Rank label should align loss with how we trade (deciles) and be fat-tail robust; expect >= raw IC. |
| 2026-06-10T21:58:02+00:00 | E0p_rank_13 | fwd_30m | rank | 13 | 570481 | 0.00965 | 1.373 | 0.00504 | Drop the 5 micro features (identity-leak risk); 13-feature universe set. Should hold IC while removing leakage. |
| 2026-06-10T21:58:17+00:00 | E_raw_13 | fwd_30m | raw | 13 | 570481 | 0.02052 | 2.976 | -0.00107 | Regression on 13 non-micro features; isolates whether micro columns were carrying (leaked) signal. |
| 2026-06-10T21:58:32+00:00 | LONGSHOT_60m_rank_13 | fwd_60m | rank | 13 | 519724 | 0.01178 | 1.436 | 0.00727 | For-fun long shot: does the 60m horizon rank model show anything different? (sparse 60m labels expected; exploratory). |

## Wave 1 findings (Modeller, 2026-06-10) — exploration, not edge

- **13 features ≡ 18 features, EXACTLY** (E_raw_13 == E0_raw_18; E0p_rank_13 == E0p_rank_18).
  The 5 micro features carry ZERO signal (99.9% NaN → LightGBM ignores them). => the
  13-feature universe set loses nothing, and the feared micro "identity-leak" is NOT
  being exploited by the current model. Production decision (drop micro) is safe.
- **Raw regression BEATS rank-as-regression-target here** (raw IC 0.0205 t2.98 canary
  -0.001 vs rank 0.0097 t1.37 canary 0.005) — surprise vs hypothesis. CAVEAT: "rank"
  here = regression on the rank value, NOT true LambdaRank. Honest next test = LGBMRanker.
  The rank canary (0.005-0.007) is mildly elevated vs raw (-0.001) — watch it.
- **60m horizon (rank) IC 0.0118 < 30m** — not apples-to-apples (60m used rank); 60m raw queued.
- NEXT QUEUE: lambdarank (LGBMRanker grouped by ts); 60m raw; vol-scaled label; daily
  cross-sectional momentum features; a GPU torch long-shot.

## Ops note: experimenter writes host files as root -> run it as host uid (fix next cycle).
| 2026-06-10T22:12:57+00:00 | E_raw_13_imp | fwd_30m | raw | 13 | 570481 | 0.02052 | 2.976 | -0.00107 | (A) Re-run raw/13 WITH gain importances to diagnose WHICH features carry the signal — start of feature-improvement work. |
| 2026-06-10T22:13:13+00:00 | E_60m_raw_13 | fwd_60m | raw | 13 | 519724 | 0.01195 | 1.338 | 0.00012 | Fair 60m comparison: RAW label, 13 features (vs the earlier 60m-rank long-shot). Does a longer horizon help raw regression? |
| 2026-06-10T22:14:16+00:00 | DIAG_nocalendar_11 | fwd_30m | raw | 11 | 570481 | -0.00426 | -0.537 | -0.00389 | (A) Importances show calendar features (day_of_week, minute_of_day) rank high, but they're constant within a cross-section so can't discriminate names. Drop calendar+micro (11 features): if within-ts IC survives, the signal is real cross-sectional; if it collapses, the IC was a time-of-day artifact. |

## CRITICAL FINDING (Modeller, 2026-06-10) — the IC was a CALENDAR ARTIFACT

Feature-importance diagnosis on raw/13 showed the top gain features are
gap_from_open, day_of_week, vwap_dev, minute_of_day, ret_5m. But day_of_week and
minute_of_day are CONSTANT within each cross-section (same for every name at a ts),
so they cannot discriminate names. Diagnostic DIAG_nocalendar_11 (drop calendar):

  raw/13 (with calendar):  IC  0.0205  t  2.98
  raw/11 (no calendar):    IC -0.0043  t -0.54   <-- IC COLLAPSES

=> The entire apparent 0.0205 IC was driven by calendar features the model used as
regime conditioners (time-of-day/day-of-week x feature interactions), over a THIN
51-day panel — almost certainly overfit, and NOT tradeable as a cross-sectional name
ranker (you can't rank names by day_of_week). The price-only cross-sectional features
have ~ZERO standalone within-ts signal right now.

IMPLICATIONS (reshape the modeling path):
- Do NOT treat the 0.0205 IC as edge — it's a calendar/regime artifact (canary is
  clean, so not leakage; it's thin-panel regime overfit). Honest baseline IC of the
  PRICE features alone ~ 0.
- The team's instinct is confirmed: we need BETTER FEATURES. Modeller (B): invent +
  collect new signals — cross-sectional daily momentum, short-horizon reversal
  interactions, order-flow (needs universe-wide trade/quote streaming = Production Eng),
  late-session/overnight structure. Price-at-30min alone isn't enough.
- Re-evaluate calendar features: keep them only as explicit regime CONDITIONERS with
  enough time depth to trust (250+ days), not as the source of "signal".
- Strengthens the case to accumulate time depth AND to pursue the overnight horizon.
| 2026-06-10T22:42:26+00:00 | E_60m_raw_nocal | fwd_60m | raw | 11 | 519724 | 0.00527 | 0.624 | 0.00179 | Modeller: 60m raw, no-calendar (11 feats). Does ANY price signal survive at the longer horizon without the calendar crutch? |
| 2026-06-10T22:42:44+00:00 | E_30m_rank_nocal | fwd_30m | rank | 11 | 570481 | 0.00211 | 0.264 | 0.00576 | Modeller: rank label, no-calendar 11 feats. Honest within-ts cross-sectional test of price features under a trading-aligned-ish loss. |
| 2026-06-11T02:13:00+00:00 | E_mom_raw_nocal_v11 | fwd_30m | raw | 11 | 570481 | -0.00426 | -0.537 | -0.00389 | KEY TEST: v1.1.0 daily-momentum features, raw, NO calendar. Does cross-sectional momentum give non-artifact within-ts IC where intraday price gave ~0? |
| 2026-06-11T02:13:17+00:00 | E_mom_raw_all_v11 | fwd_30m | raw | 18 | 570481 | 0.02052 | 2.976 | -0.00107 | v1.1.0 momentum + all (incl calendar). Compare to nocalendar to see momentum's standalone contribution vs the calendar crutch. |
| 2026-06-11T02:13:32+00:00 | E_mom_60m_raw_nocal_v11 | fwd_60m | raw | 11 | 519724 | 0.00527 | 0.624 | 0.00179 | v1.1.0 momentum at 60m horizon, no calendar (momentum decays slower than 30m noise). |
| 2026-06-11T02:28:54+00:00 | E_mom_raw_nocal_v11 | fwd_30m | raw | 19 | 568162 | 0.00648 | 0.996 | 0.00522 | KEY TEST: v1.1.0 daily-momentum features, raw, NO calendar. Does cross-sectional momentum give non-artifact within-ts IC where intraday price gave ~0? |
| 2026-06-11T02:29:13+00:00 | E_mom_raw_all_v11 | fwd_30m | raw | 21 | 568162 | 0.0133 | 2.061 | 0.00558 | v1.1.0 momentum + all (incl calendar). Compare to nocalendar to see momentum's standalone contribution vs the calendar crutch. |
| 2026-06-11T02:29:28+00:00 | E_mom_60m_raw_nocal_v11 | fwd_60m | raw | 19 | 519524 | 0.00593 | 0.676 | -0.00529 | v1.1.0 momentum at 60m horizon, no calendar (momentum decays slower than 30m noise). |

## Momentum finding (Modeller, 2026-06-11) — real CONTRIBUTOR, but not yet edge

v1.1.0 daily-momentum, correctly run on the 21-feature panel (the first run was a stale-
code bug: it scored v1.0.0 — purged):

  E_mom_raw_nocal_v11 (19f, no cal): IC  0.0065  t 1.00  canary 0.0052
     top: gap_from_open, mom_1d(2.3), mom_1d_rel(2.1), vwap_dev, range_pct
  E_mom_raw_all_v11   (21f, w/ cal): IC  0.0133  t 2.06  canary 0.0056   (calendar back on top)
  E_mom_60m_nocal     (19f, no cal): IC  0.0059  t 0.68  canary -0.0053

READ (honest):
- Momentum IS the first non-calendar feature family that actually CONTRIBUTES: mom_1d /
  mom_1d_rel rank among the top features, and adding momentum flips the no-calendar IC
  from -0.004 (price-only, v1.0.0) to +0.0065. Orthogonal signal exists.
- But it is NOT a validated edge: on the 30m no-calendar test the shuffle CANARY (0.0052)
  is nearly as high as the real IC (0.0065), and t~1.0. The canary defines the noise/
  overfit floor (~0.005) on this 51-day panel; momentum barely pokes above it. 60m has a
  clean canary (-0.005) but t=0.68. Neither clears the bar.
- Verdict: momentum is the best lead so far and worth keeping, but the binding constraint
  is TIME DEPTH (51 days; 10-day momentum has ~5 independent samples), exactly as
  predicted. Don't trade it. NEXT: true lambdarank + vol-scaled label (need research.py
  paths), and keep accumulating days; re-run the gauntlet as depth grows.
- METHODOLOGY NOTE for QA/Modeller: canary ~±0.005 is the IC estimation-noise band here;
  treat any |IC| < ~0.005 as indistinguishable from zero. Make that an explicit gate.
| 2026-06-11T04:11:33+00:00 | E_mom_raw_nocal_v11 | fwd_30m | raw | 19 | 568162 | 0.00648 | 0.996 | 0.00522 | KEY TEST: v1.1.0 daily-momentum features, raw, NO calendar. Does cross-sectional momentum give non-artifact within-ts IC where intraday price gave ~0? |

## NET-OF-COST GATE (2026-06-11) — momentum LOSES money after costs

The harness now reports a net-of-cost L/S backtest (quantlib.backtest.long_short_backtest).
Momentum (E_mom_raw_nocal_v11), 30-min cadence:
  gross +2.76 bps/period | NET -1.6 bps | Sharpe_net -2.0 | turnover 2.18/period
  BREAKEVEN one-way cost = 1.27 bps  vs ~2 bps realistic (half a ~4 bps round-trip spread)
=> Even ignoring the noise-floor IC, the strategy is NET-NEGATIVE: the signal can't clear
the spread at full 30-min turnover. The owner-audit's call is quantified: LOWER TURNOVER
(longer horizon) beats any feature here. "Beats breakeven cost" is now a hard gate on every
experiment. NEXT: build + test the OVERNIGHT horizon (far lower turnover) under this gate.
| 2026-06-11T04:39:58+00:00 | E_overnight_raw_nocal_v11 | overnight | raw | 19 | 49225 | 0.09376 | 2.653 | 0.00958 | Overnight (close->next-open) under the net-of-cost gate: ~1 rebalance/day = far lower turnover; should clear breakeven where 30-min could not. raw, no-calendar, v1.1.0 (momentum+price at 15:30). |
| 2026-06-11T04:40:07+00:00 | E_overnight_raw_all_v11 | overnight | raw | 21 | 49225 | 0.06849 | 1.757 | 0.02403 | Overnight, all features incl calendar/momentum, v1.1.0. Compare to nocalendar. |

## OVERNIGHT under the cost gate (2026-06-11) — high IC that is NOT money (fat-tail/gap)

Built a close->next-open label (quantlib.labels.overnight_return_series; backfiller
build-overnight-labels; assigned to each day's last cadence ts). E_overnight_raw_nocal_v11
(v1.1.0, 50 days):
  rank-IC 0.094 (t 2.65) BUT gross -0.23bps/period, NET -0.31bps, Sharpe_net -0.84,
  turnover 3.98, breakeven -5.9bps.
READ (honest): a strongly positive rank-IC with NEGATIVE dollar P&L is the tell that the
ordering doesn't translate to money — fat-tailed overnight returns (earnings GAPS): extreme
positive gaps in the low-pred (short) leg blow up the equal-weight basket. IC is a
MISLEADING metric overnight; the net-of-cost L/S backtest exposed it. Overnight-as-built is
net-negative AND its IC is untrustworthy. NEXT (Modeller): vol-SCALE / winsorize the
overnight label, filter earnings-gap days, and judge on net P&L (not IC). Also: turnover
~4/period is too high for "overnight" — the staleness/rebalance logic needs the daily cadence.
Cost gate working as intended: it stopped a 0.094 IC from being mistaken for edge.
| 2026-06-11T16:10:08+00:00 | DEEP_overnight_volscaled_nocal_v11 | overnight | vol_scaled |  | 0 |  |  |  | DEEP panel: overnight + VOL-SCALED label (stops ranking volatility instead of alpha) + no-calendar. Judge NET P&L. |
| 2026-06-11T16:10:19+00:00 | DEEP_30m_volscaled_nocal_v11 | fwd_30m | vol_scaled |  | 0 |  |  |  | DEEP panel: 30m vol-scaled, no-calendar — does vol-normalization rescue any intraday signal at real depth? NET P&L. |
| 2026-06-11T16:40:20+00:00 | DEEP_overnight_lambdarank_nocal_v11 | overnight | lambdarank |  | 0 |  |  |  | DEEP: overnight + true LGBMRanker (lambdarank, grouped by ts) + no-calendar. Loss aligned to decile trading. NET P&L. |
| 2026-06-11T16:40:32+00:00 | DEEP_30m_lambdarank_nocal_v11 | fwd_30m | lambdarank |  | 0 |  |  |  | DEEP: 30m lambdarank, no-calendar — the trading-aligned loss on real depth. NET P&L. |
| 2026-06-11T17:40:33+00:00 | E_overnight_raw_nocal_v11 | overnight | raw |  | 0 |  |  |  | Overnight (close->next-open) under the net-of-cost gate: ~1 rebalance/day = far lower turnover; should clear breakeven where 30-min could not. raw, no-calendar, v1.1.0 (momentum+price at 15:30). |
| 2026-06-11T17:40:34+00:00 | E_overnight_raw_all_v11 | overnight | raw |  | 0 |  |  |  | Overnight, all features incl calendar/momentum, v1.1.0. Compare to nocalendar. |

## DEEP OVERNIGHT under the cost gate (2026-06-11) — first positive net result, BUT canary-contaminated

612-day clean panel (570,590 overnight rows, split-only basis). Ranked by sharpe_net:
  lambdarank: IC 0.029 net +0.082bps sharpe_net +0.50 breakeven 4.11bps  CANARY 0.0097
  rank:       IC 0.023 net +0.027bps sharpe_net +0.18 breakeven 2.72bps  CANARY 0.003
  vol_scaled: IC 0.008 net -0.052bps sharpe_net -0.34 breakeven 0.75bps  CANARY 0.0067
  raw:        IC 0.004 net -0.060bps sharpe_net -0.37 breakeven 0.55bps  CANARY -0.002
EXCITING: lambdarank+overnight is the FIRST config to clear breakeven NET-of-cost (positive net
P&L, sharpe_net +0.50) — the thesis (lower turnover + loss alignment) held where 30m died.
BUT NOT TRUSTED: the shuffle canary RISES with ranking sophistication (raw -0.002 -> lambdarank
0.0097). At ~480 test days, canary 0.0097 is ~6sigma from 0 -> NOT noise = leakage/selection
artifact the model exploits even on shuffled labels. So a large part of the apparent IC is
artifact, not alpha. PLUS survivorship (delisted absent, upward bias) + earnings-gap noise NOT
excluded. VERDICT: a real LEAD, not edge. NEXT GATES (in order): (1) EXPLAIN the elevated
ranking-canary (leakage? group-structure overfit? a feature with persistent cross-sectional
selection?) — if it's leakage the result is fake; (2) earnings exclusion (FMP) + survivorship
handling; (3) deflate for the 8-config multiple test; (4) lockbox/OOS. Do NOT trade it.

## DEEP 30m intraday baseline (2026-06-11) — real signal, CLEAN canary, but net-NEGATIVE (turnover)
612-day panel (6.15M rows). nocalendar v1.1.0, cost-gated:
  rank IC 0.032 sharpe_net -3.42 breakeven 1.33bps canary -0.003
  raw  IC 0.024 sharpe_net -3.57 breakeven 1.29bps canary -0.004
  vol  IC 0.024 sharpe_net -4.05 breakeven 1.26bps canary -0.004
  lrank IC 0.001 sharpe_net -1.49 breakeven 0.61bps canary -0.003
KEY: this DEBUNKS the 51-day "calendar artifact / price-only IC~0" conclusion. With real depth,
nocalendar 30m IC is 0.024-0.032 with a CLEAN canary (~ -0.004) -> a REAL intraday cross-
sectional signal exists in price features (the 51-day collapse was thin-panel noise). BUT it's
net-NEGATIVE (breakeven ~1.3bps < ~2bps cost) -> turnover kills it at 30m cadence. Real, not
tradeable. CONTRAST WITH OVERNIGHT: 30m canaries are CLEAN; the overnight canaries were ELEVATED
(up to 0.0097) exactly on the configs that cleared breakeven -> the overnight "win" is where the
integrity check is dirty = likely artifact, not alpha. (Canary investigation in progress.)

## CLEAN DEEP OVERNIGHT (2026-06-11, de-fragmented = 612 daily cross-sections, deterministic)
  lambdarank IC 0.030 sharpe_net +1.95 breakeven 15.0bps turn 1.40 canary 0.0077
  rank       IC 0.017 sharpe_net +0.62 breakeven 3.75bps turn 2.94 canary 0.001
  raw        IC 0.007 sharpe_net +0.27 breakeven 2.69bps turn 2.93 canary -0.007
  vol_scaled IC 0.006 sharpe_net -0.15 breakeven 1.76bps turn 3.07 canary -0.006
De-fragmentation (one 15:30-ET cross-section/day) DROPPED lambdarank turnover 4.0->1.40 ->
breakeven 4.1->15.0bps, sharpe_net 0.50->1.95. Strongest result yet. BUT lambdarank canary
(0.0077) is still highest, and the prior probe showed the model partly ranks a PERSISTENT
per-symbol component (canary-persistence 0.34) = likely SURVIVORSHIP. GATE: neutralize the
persistent per-symbol prediction bias -> does within-symbol TIMING alpha survive? (running)

## SURVIVORSHIP GATE — overnight "edge" is SURVIVORSHIP, not alpha (2026-06-11, DEFINITIVE)
Clean de-fragmented overnight lambdarank: RAW sharpe_net +2.11 (breakeven 15.9bps). Per-symbol-
DEMEANED (remove persistent per-symbol bias = the survivorship component): sharpe_net -0.21,
net -0.0001, breakeven 1.7bps. COLLAPSE. => the entire overnight result was the model ranking
ex-post SURVIVORS (persistent per-symbol drift, known only because they survived 2.5yr), NOT
within-symbol overnight TIMING. Real timing alpha ~ ZERO.

### DEEP-PANEL EDGE INVESTIGATION — HONEST CONCLUSION
Price-only cross-sectional features have NO tradeable edge, gated rigorously (cost + canary +
de-fragmentation + survivorship neutralization):
- 30m intraday: REAL signal (IC 0.024-0.032, clean canary on 612 days) but NET-NEGATIVE after
  costs (breakeven ~1.3bps < ~2bps) -> uneconomic at turnover.
- overnight: apparent strong result (sharpe +2.1) was SURVIVORSHIP -> ~0 timing alpha.
PATH TO EDGE is BETTER DATA, not more price-feature modeling: universe-wide ORDER-FLOW /
microstructure (the Architect's sharded trade/quote ingestion — we only stream 10 symbols), and
delisted-name backfill to test overnight survivorship-free. The EXECUTION infra is proven (place/
manage/terminate bets live-paper), so when real edge appears we can trade it. No false edge shipped.

## PRE-REGISTRATION (Modeller, 2026-06-11) — clean-panel battery, hypotheses BEFORE the data

M1 task #4. The ~600-day panel the verdict above was computed on was ~21% contaminated
(207/1000 members were ETFs / leveraged-inverse / VIX-futures funds, ranked cross-sectionally
against single stocks). prod-architect is rebuilding the clean equities-only panel (~790 names).
Per pre-registration discipline I commit these falsifiable predictions NOW, so a clean result
cannot be rationalized after the fact. The re-run is ONE command: `experiments/battery.py`
(net-of-cost L/S + shuffle canary + de-fragmented overnight labels + per-symbol survivorship
neutralization, deterministic).

PRIMARY PREDICTION (confidence ~70%): **"price-only has no tradeable edge" HOLDS on clean data.**
The fund contamination did NOT mask a real edge. 30m = real signal but net-negative after costs;
overnight = survivorship, not timing.

Per-gate, falsifiable:
1. **30m intraday IC** — two competing effects. Leveraged/inverse ETFs are the highest-|return|
   names each cross-section and mechanically momentum/reversal-predictable (H1a: they INFLATE IC,
   so clean IC FALLS toward the noise floor) vs. their leverage/decay returns are orthogonal noise
   that dilutes within-ts rank correlation (H1b: clean IC RISES). I lean H1a (~60/40). Either way:
   **PREDICTION — 30m stays NET-NEGATIVE after costs (breakeven < ~2bps).** The "real but uneconomic
   at 30m turnover" verdict holds.
2. **Trading cost / turnover** — ETFs are the tightest-spread, cheapest names in the basket.
   Removing them makes the basket trade only single names => effective cost RISES, breakeven bar
   gets HARDER. **PREDICTION — the 30m net-of-cost picture gets slightly WORSE, reinforcing
   "uneconomic."** (This is the asymmetry that makes a clean 30m edge unlikely.)
3. **Overnight survivorship** — leveraged/inverse funds carry the STRONGEST persistent per-symbol
   overnight drift (vol-decay: structurally negative for UVXY/VXX/SQQQ, positive for TQQQ/UPRO in
   an up-market). They were a large chunk of the "rank ex-post survivors" artifact. **PREDICTION —
   on clean equities the RAW overnight sharpe DROPS and the gap between raw and per-symbol-demeaned
   (survivorship-out) sharpe NARROWS; timing alpha stays ~0.** The survivorship diagnosis was right
   and was not itself a fund artifact.
4. **Overnight ranking canary** — the elevated lambdarank canary (0.0077) partly reflects the
   persistent cross-sectional selection structure of always-extreme funds. **PREDICTION — removing
   funds LOWERS the overnight ranking-canary (cleaner).** TRIPWIRE: if the canary STAYS elevated on
   clean equities, the selection/leakage artifact is intrinsic to the features (not the funds) —
   that reframes the lambdarank result as fake regardless of panel, and must be chased down.
5. **NW t / cross-section size** — clean panel has ~790 names/ts vs ~1000; per-ts IC is slightly
   noisier. Expect t-stats to move modestly; not decisive.

THE RESULT THAT WOULD CHANGE EVERYTHING (~30% tail I am NOT dismissing): removing fund NOISE lifts
clean-equity 30m IC enough that a LOWER-TURNOVER variant (60m horizon, or top/bottom-decile-only
with hysteresis to cut the 2.2/period turnover) clears breakeven net-of-cost. So the clean battery
must ALSO probe lower-turnover intraday variants, not merely reproduce the 30m/overnight grid — a
real edge here would most plausibly show up as "modest IC + low turnover," not "high IC." If that
appears, it does NOT get called edge until it passes the full M3 gate (NW t>3, clean canary,
positive net-of-cost, survives survivorship neutralization, multiple-testing deflated).

### PRE-REGISTRATION ADDENDUM (Modeller, 2026-06-12) — wider clean universe + recomputed labels

Manager relayed two facts from prod-architect's rebuild. Folding them in WHILE STILL BLIND to results.

**The clean panel differs from the dirty one in TWO directions, not one** (~885-900 equities/date,
not the ~790 I assumed): 213 funds REMOVED **and** ~160 real equities/date ADDED that the 1000-cap
had displaced. Those added names are BY CONSTRUCTION lower-ADV than the funds they replace.
6. **Added lower-ADV names — IC:** ambiguous and probably modest. More genuine single-name dispersion
   could RAISE within-ts IC (more nameable alpha), but lower-ADV names have noisier prices/features,
   which adds cross-sectional noise. **PREDICTION — small net effect on IC magnitude; I do NOT expect
   the added names to manufacture a clean intraday edge.**
7. **Added lower-ADV names — COST (the decisive one):** lower-ADV ⇒ WIDER spreads. The battery charges
   a flat 2bps one-way; for the newly-included tail that assumption is now OPTIMISTIC. The L/S basket
   trades top/bottom deciles (~89 names/leg), and volatile lower-ADV names are MORE likely to sit in
   those extremes ⇒ higher real cost on exactly the names we trade. **PREDICTION — net-of-cost for 30m
   gets HARDER, not easier; "real but uneconomic at turnover" is reinforced.** (Flag: a future battery
   variant should cost per-name by ADV/spread, not flat 2bps — the flat charge flatters the result.)
8. **Breadth ⇒ t-stat MECHANICAL inflation (tripwire):** ~885-900 names/ts gives less-noisy per-ts IC
   estimates than the 1000-cap's effective breadth, so the Newey-West t can RISE even if true IC is
   unchanged. **DO NOT read a higher t as stronger edge.** Judge on IC MAGNITUDE vs the canary AND on
   breakeven_cost_bps — never on t alone. A t that rises while IC and breakeven stay flat = breadth, not alpha.
9. **Recomputed labels (all 3 horizons) — demean baseline shifts:** labels are excess-vs-universe-median,
   so changing membership changes every value. Funds (esp. leveraged/inverse) had extreme returns that
   pulled the median/tails; removing them makes the demean baseline more representative ⇒ the label
   distribution should TIGHTEN (less fat-tailed). **PREDICTION — overnight's fat-tail/earnings-gap blow-up
   eases slightly, but earnings gaps and the SURVIVORSHIP component remain; overnight timing alpha still ~0.**

**Stale-label tripwire (verified mechanics):** `labels_pkey = (symbol, ts, horizon)`, 0 dup rows today,
so a recompute that DELETE-then-inserts (or ON CONFLICT DO UPDATE) replaces cleanly. The battery's
`load_panel` runs a fresh SQL JOIN on every invocation — it caches NOTHING — and the JOIN is
feature_vectors-DRIVEN (filtered to the clean set_version), so any stale label rows for DROPPED names
are inert (no fv row to join). The ONE real risk: if the recompute only INSERTS the newly-added names
and does NOT overwrite the EXISTING names' values, those existing rows keep their dirty-universe-demeaned
values = silent contamination. **Pre-run check I will run before trusting the clean battery:**
`SELECT horizon, min(computed_at), max(computed_at) FROM labels GROUP BY 1` — min(computed_at) must be
AFTER the rebuild timestamp for ALL three horizons (proves every value was recomputed, not just new rows).

**Net effect on the PRIMARY prediction:** UNCHANGED / mildly reinforced (~70%). The cost asymmetry from
the added lower-ADV names pushes AGAINST a tradeable 30m edge, and the tighter labels don't rescue
overnight from survivorship. The result that would still flip it is the same low-turnover tail flagged above.

## DELISTED-NAME BACKFILL — research requirements spec (Modeller → prod-architect, task #9)

WHY IT MATTERS (what verdict it changes): the overnight result collapsed under per-symbol demean
(sharpe +2.1 → ~0). But per-symbol demean is a CONSERVATIVE PROXY for survivorship — it removes ALL
persistent per-symbol drift, which also kills any real persistent alpha. The HONEST test is to put
the delisted names BACK into each historical cross-section so the panel contains the names that
actually existed on that date (some later delisted), removing the upward bias AT THE SOURCE instead
of via demean. Delisted names are disproportionately the LOSERS (bankruptcies, distressed M&A) =
exactly the short-leg names whose absence inflates L/S and overnight results.

REQUIREMENTS (minimum that would actually change a verdict):
- NAMES: US equities that QUALIFIED for our liquid universe (price>$5, ADV$>$10M) at any date in
  the window (2024-01-02 → present) but have since delisted/merged/acquired/gone bankrupt. Estimate
  ~50-120 names over ~2.5yr (liquid-equity delist rate ~2-4%/yr). Don't need every micro-cap; the
  liquid delisted set is what de-biases the cross-section.
- DEPTH: split/div-ADJUSTED DAILY OHLCV from each name's first in-window date through its delist date.
  Minute bars NOT required for the overnight/daily survivorship test (overnight = close→next-open).
  Minute bars are a lower-priority follow-on only if we later test INTRADAY survivorship-free.
- POINT-IN-TIME UNIVERSE: `universe_membership` history must include delisted names on the dates they
  qualified. Today it's built from SURVIVING names' backfilled bars only — that IS the bias source.
  So the backfill must feed BOTH bars and the universe screen, PIT.
- SOURCE: Alpaca drops delisted symbols. Need a source carrying dead tickers + delist dates — Polygon,
  the existing FMP key, or Sharadar/Norgate. Feasibility/cost is prod's call; I need adjusted daily
  bars + the delisting date per name.
- ACCEPTANCE: re-run the overnight battery with delisted names included PIT and NO per-symbol demean.
  If net-of-cost sharpe stays positive survivorship-free → a genuine lead (escalate to full M3 gate).
  If it collapses like the demean proxy → the no-edge verdict is confirmed survivorship-FREE, not just
  by a conservative proxy. Either outcome is a real result.

## OFI 50-NAME PILOT — pre-registered design + TRIGGER (Modeller, 2026-06-12) — NOT runnable yet

Manager suggested a cheap early read on order-flow (v1.2.0 OFI features: ofi_5m/15m/30m, signed_vol_z_30)
on the ~50-name capture, before the M2 500-name scale-up, to de-risk/redirect the edge bet. I AGREE it's
worth a slot — but it is NOT runnable now and must be TRIGGER-GATED to avoid a false read:
- DATA STATUS (2026-06-12): trade_agg/quote_agg cover **52 symbols × 2 days** only; v1.2.0
  feature_vectors = **0 rows (never computed)**. Two days is pure noise.
- PREREQUISITES (all three): (a) ≥ ~10 trading days of OFI capture accrued (≥ ~2-3 wks wall-clock);
  (b) prod builds v1.2.0 feature_vectors over the 52-name OFI panel (currently unbuilt); (c) experimenter
  image fixed (task #8).
- DESIGN when it runs: cost-gated battery, fwd_30m + fwd_60m, on the 52-name cross-section, comparing
  three feature subsets to ISOLATE OFI's marginal value — price-only (nocalendar) vs price+OFI vs
  OFI-only — same IC + shuffle-canary + net-of-cost gates. (Requires extending battery.py with an
  OFI-aware feature-subset selector; trivial, do it when data lands.)
- WHAT IS / IS NOT A SIGNAL: a 50-name cross-section gives ~25/leg deciles = very noisy; this is a
  CURIOSITY read, NOT a verdict. A real positive sign = price+OFI IC materially > price-only with a
  clean canary AND OFI features ranking high in importance. Anything marginal = "wait for the 500-name
  scale-up," not "OFI works." NO edge claim comes out of 50 names regardless.
- PURPOSE: a cheap early YES/NO/MAYBE to inform whether M2's 500-name investment is well-aimed.

## BACKLOG (post-M1): per-name measured-cost battery variant (Modeller ↔ execution-risk task #7)

The M1 clean re-run stays FLAT 2bps one-way for apples-to-apples with the contaminated run (verdict
notes the optimism caveat). Post-M1, replace the flat charge with a cost MODEL calibrated to measured
fills (execution-risk's `execution_slippage` / `execution_slippage_daily`, commit 4c3c46a).

DESIGN:
- Per-day mean alone is INSUFFICIENT — it can't separate the wide-spread short-leg microcaps from
  liquid names, which is the entire point (pre-registered prediction: added lower-ADV names cost MORE
  and that's what kills 30m net-of-cost). A single daily scalar just shifts the flat assumption.
- Right granularity = **per-name one-way cost keyed by liquidity**. Consume per-LEG `execution_slippage`
  rows (slippage_bps, arrival_src) joined with each name's ADV$ + price AT SUBMIT, filtered to
  `arrival_src='nbbo'` (bar_proxy is intra-minute noise, ±50-125bps, unusable). From those, fit a
  BUCKETED lookup `cost_bps(ADV_bucket × price_bucket)` (or a simple monotone regression), then apply
  per-name across the full ~890 universe by each name's PIT ADV/price — generalizes measured cost to
  names we've never traded.
- HARNESS CHANGE: extend `quantlib.backtest.long_short_backtest` to accept `cost_bps_by_symbol`
  (dict or callable) and charge `sum(cost_bps[sym] * abs(Δweight[sym]))` instead of `flat * turnover`.
  Backward-compatible: scalar default = today's behavior.
- ACCEPTANCE: re-run the battery with the measured per-name model; if a config that cleared FLAT-2bps
  breakeven NO LONGER clears its per-name measured cost, the flat gate was too lenient (expected for
  microcap-heavy baskets). This only TIGHTENS the M3 net-of-cost bar — never loosens it.
- Format request to execution-risk: per-leg slippage_bps + ADV$ + price at submit (nbbo only). I build
  the bucketed curve; the daily aggregate is fine as a sanity scalar but not the model input.

### PRE-REGISTRATION CORRECTION (Modeller, 2026-06-12) — clean panel is ~715 names, NOT ~885

prod-architect corrected the panel scope (the earlier "~885/date, +160 newly-included names" was a
misread of a stale ETF-included run). GROUND TRUTH for the clean v1.1.1 panel:
- **set_version = v1.1.1** (same 21-feature v1.1.0 contract, clean equities-only membership).
- **Cross-section ≈ 715 equities/date, NOT ~885.** The clean equity set ≈ the old equity portion
  (~715 vs ~723) — contamination was ~210 ETFs ADDED ON TOP of the equities, now removed. So NO new
  equities enter the ranking. The dirty panel was ~715 equities + ~210 funds ≈ the 1000-cap.
- **Labels = DELETE-then-insert full overwrite, all 3 horizons**, re-demeaned over the clean ~715
  cross-section; `computed_at=now()` → my acceptance gate passes. Battery v1.1.1 features ⨝ fresh
  labels. Do NOT battery old v1.1.0 feature rows (their matching dirty labels are being overwritten →
  inconsistent). The canonical "before" is the existing v1.1.0 results.jsonl rows.

WHAT THIS CHANGES IN MY PRE-REGISTRATION:
- **SUPERSEDED — addendum hyps #6 and #7** (effects of "added lower-ADV names"): VOID, no names added.
  The flat-2bps-is-optimistic caveat SURVIVES but now rests solely on execution-risk's measured-microcap
  finding (real one-way cost on the names we trade may exceed 2bps), NOT on any universe-composition change.
- **CORRECTED — addendum hyp #8 (breadth/t-stat), DIRECTION REVERSES:** the cross-section SHRINKS
  1000→715 (fewer names/ts), so per-timestamp IC is NOISIER and t-stats may mechanically FALL, not rise.
  The tripwire STANDS but flips: do NOT read a t-stat DROP as "signal weakened" — ~285 fewer names/ts
  alone lowers t at unchanged true IC. Still judge on IC magnitude vs canary AND breakeven, never t.
- **REAFFIRMED as the substantive change — re-demeaned labels (was hyp #9):** every equity's
  excess-return label shifts because the cross-sectional median/ranking no longer includes fund returns.
  This + the original fund-removal hypotheses (#1-#5: funds were the highest-|return|, mechanically-
  predictable names dominating the traded extremes) are now the WHOLE delta. My original predictions
  hold: 30m real-but-net-negative; overnight = survivorship; primary verdict "no tradeable edge holds"
  UNCHANGED at ~70%. The result that would flip it remains the low-turnover tail, gated by full M3 criteria.

## OFI CLOSE-MINUTE EXCLUSION — feature-definition spec (Modeller → prod featurestore, 2026-06-12)

QA settled-day trade-agg proof at 52 names (f868896): core RTH aggregation is trustworthy (n_trades
within-2% = 98.05% / corr 0.9997; tick-rule SIGN agreement 99.82%) — the OFI thesis survives its first
hard test. BUT live-vs-REST n_trades-within-2% COLLAPSES at the close: 93% at 15:00 ET → 14% at 16:00 ET
(closing cross / MOC + late & out-of-sequence prints). Any OFI feature consuming those minutes is
computed on untrustworthy LIVE data → live/backfill skew exactly where parity matters most.

SPEC (binds in the SHARED featurestore path so live == backfill, per parity discipline):
1. **Drop minutes ≥ 15:50:00 ET from the trade_agg input to OFI aggregation.** OFI windows (ofi_5m,
   ofi_15m, ofi_30m, signed_vol_z_30) MUST NOT include any minute in [15:50, 16:00] ET. A window
   anchored before 15:50 truncates at 15:50 (use the clean minutes; if too few remain for the window,
   emit NaN rather than a partial-window value).
2. **Do NOT compute OFI features at any cadence timestamp ≥ 15:50 ET.** With the current 30-min cadence
   the last OFI-bearing cadence is 15:30 ET (whose ofi_30m window 15:00–15:30 is fully clean). 16:00 has
   no forward intraday return anyway.
3. **16:00 ET (closing cross) stays PERMANENTLY excluded** for intraday OFI even after prod fixes
   close-hour aggregation — it's a distinct auction liquidity event, not continuous order flow.
RATIONALE for the 15:50 boundary: the MOC imbalance period begins ~15:50 ET and the cross prints at
16:00; 15:50 is the conservative line that avoids both. We lose only the final 10 min of OFI signal —
the most auction-distorted, least-trustworthy slice.
REFINEMENT (needs data): I only have 15:00 (93%) and 16:00 (14%). Requesting per-minute within-2%
parity for 15:30 / 15:45 / 15:55 ET. **15:30 is material** — the overnight label AND the last intraday
cadence both anchor there; if 15:30 parity is degraded that's a bigger problem than the close window.
Once prod's close-hour fix lands, re-measure 15:50–15:59 and potentially reclaim those minutes (16:00
stays out). Scope = OFI features only; price/bar features are far more robust (bars validated well) and
are a separate, lower-concern question for QA.
PILOT: the trigger-gated 50-name OFI pilot inherits this exclusion — all its cadence points anchor
≤ 15:30 ET so no OFI window touches the bad minutes. Pre-registered here so the pilot never consumes them.

## OFI PILOT — trigger correction + at-scale-parity prerequisite (Modeller, 2026-06-12)

QA's 15:30 answer (b137128): 15:30/15:45/15:55 ET = 100%/100% within-2%/sign — the overnight-label
anchor is CLEAN (overnight verdict is NOT parity-tainted), and my ≥15:50 OFI exclusion line is confirmed
safe (do not move earlier; 16:00 stays out — and backfill trade-agg is RTH-bounded so post-close OFI has
no validation reference at all, reinforcing the hard close-stop). NOTE: this concerns the LIVE stream;
the M1 price-only battery is all backfill-sourced and was never affected.

CAVEAT that revises the pilot (NOT M1): QA found the proof was thinner than labeled — the live stream
captured only ~10 of 50 names until the 10→50 subscription expansion deployed ~15:51 ET on 6/11. So the
parity numbers are a ~10-name FULL-DAY proof + a ~10-minute 50-name window. Sign quality on captured
names is solid (the hard part), but AT-SCALE (50-name) parity is NOT yet proven.

REVISIONS to the trigger-gated OFI pilot:
- **NEW prerequisite (d): at-scale 50-name trade-agg parity PROVEN on a settled session** (QA invariant)
  before any pilot result is trusted — sign quality is proven on ~10 names, not 50.
- **CORRECTED trigger clock:** the "≥10 OFI days" count starts from the first CONFIRMED full-session
  50-name capture day (~6/12), NOT from 6/10. Earliest pilot ≈ 6/26 (was ~6/24). Don't count the
  partial-capture days.
- Unchanged: needs v1.2.0 feature_vectors built (task #10), experimenter fixed (done), and the ≥15:50
  OFI exclusion baked into the shared featurestore. Still a curiosity read at 50 names, never a verdict.

## ★ CLEAN v1.1.1 VERDICT (Modeller, 2026-06-12) — price-only STILL has NO tradeable edge (honest, fund-free)

Ran experiments/battery.py FULL-depth on the CLEAN equities-only panel: set_version=v1.1.1, 5,525,040
feature rows / 613 days / 785 symbols (per-date breadth ~742), 0 ETFs/funds, labels DELETE-then-insert
re-demeaned over the clean cross-section (acceptance gate PASSED: min(computed_at) 2026-06-12 06:43Z all
3 horizons, post-rebuild). 4 gates: net-of-cost L/S + shuffle canary + de-fragmented overnight labels +
per-symbol survivorship neutralization. Deterministic (host quantlib mounted; image had VOL_FLOOR).

RESULTS (price-only = nocalendar 19 feats; net/sharpe per-period; breakeven = one-way bps the signal
absorbs before net<=0; SURV-OUT = per-symbol-demeaned re-backtest):

  fwd_30m    IC      NW_t   canary    net        sharpe  breakeven  turn   SURV-OUT sharpe
  raw        0.0270  19.99  -0.0018   -0.000183  -3.46   1.42bps    3.13   -3.51
  rank       0.0318  21.40  -0.0008   -0.000184  -3.24   1.44bps    3.25   -3.34
  vol_scaled 0.0268  19.79  -0.0014   -0.000208  -4.15   1.37bps    3.27   -3.64
  lambdarank 0.0010   0.33  -0.0015   -0.000238  -2.01   0.58bps    1.67   -6.88

  overnight  IC      NW_t   canary    net        sharpe  breakeven  turn   SURV-OUT sharpe
  raw        0.0142   1.70  -0.0056    0.000336   0.58   3.20bps    2.95   -1.79
  rank       0.0189   2.19   0.0001    0.000265   0.39   2.91bps    3.12   -1.20
  vol_scaled 0.0076   1.03  -0.0046   -0.000344  -0.66   0.97bps    3.14   -1.68
  lambdarank 0.0358   2.80   0.0020    0.001700   1.66   9.65bps    2.25   -0.35

VERDICT: ALL 8 configs => NO tradeable edge. The price-only "no edge" conclusion HOLDS on clean,
fund-free, re-demeaned data. **The 21% fund contamination did NOT mask or fake a real edge.**

HONEST READ (mechanism, unchanged from the contaminated run):
- **30m intraday: REAL signal, NOT economic.** IC 0.027-0.032 with a CLEAN canary (~ -0.001) and huge
  NW t (~20 over 613 days) = a genuinely, reliably non-zero cross-sectional intraday price signal. But
  net-NEGATIVE: breakeven 1.37-1.44bps < ~2bps realistic one-way cost. Turnover (~3.1/period) kills it.
  Removing funds barely moved the IC (0.024->0.027) — funds were neither inflating nor diluting it.
  SURV-OUT also negative => not even a per-symbol-drift artifact; just uneconomic.
- **overnight: apparent win is SURVIVORSHIP, not timing.** lambdarank shows net +0.0017, sharpe 1.66,
  breakeven 9.65bps (clears cost) — but per-symbol demean COLLAPSES it to sharpe -0.35 / net -0.00016.
  Same story as contaminated. The model ranks ex-post survivors' persistent drift, not overnight timing.
  Timing alpha ~ 0 on every overnight config (all SURV-OUT sharpe negative).

PRE-REGISTRATION SCORECARD (predictions logged BEFORE seeing this, commits 8bc0bbd/411831c):
- PRIMARY (~70%, "no tradeable edge holds"): ✅ CONFIRMED — 8/8 no edge.
- 30m stays net-negative regardless of IC direction: ✅ (IC ~unchanged, net-negative).
- overnight = survivorship, timing ~0: ✅ (SURV-OUT collapses every config).
- H4 "removing funds LOWERS the overnight ranking canary": ✅ lambdarank canary 0.0077->0.0020. The
  TRIPWIRE (canary stays elevated => intrinsic feature leakage) did NOT trigger — it dropped, and the
  survivorship gate independently kills the residual. Clean.
- Breadth tripwire: ✅ in PURPOSE (NW t ballooned to ~20 but I pre-committed to judge BREAKEVEN not t,
  and breakeven<cost => no edge). MINOR MISS on direction: I predicted t might FALL (cross-section
  933->742); instead the 613-day time depth dominated and t rose. The lesson held: t is not edge.
- The "would-change-everything" low-turnover tail: did NOT materialize. lambdarank cut 30m turnover to
  1.67 but IC collapsed to 0.001; overnight lambdarank cleared breakeven but was survivorship. No
  price-only config produced survivorship-free positive net.

CONCLUSION (unchanged, now TRUSTWORTHY not contaminated): price-only cross-sectional features have NO
tradeable edge under the 4-gate battery. PATH TO EDGE remains BETTER DATA — universe-wide ORDER-FLOW
(v1.2.0 OFI, gated on M2 scaling + the 50-name pilot) and delisted-name backfill (to test overnight
survivorship-free at the source rather than via the conservative demean proxy). No false edge shipped.

### SENSITIVITY / CAVEAT — backfill split-adjustment discontinuity (11 names), verdict STABLE (2026-06-12)

prod-architect found a backfill split-adjustment discontinuity (chasing the KLAC 10× anomaly): 11/785
names (KLAC, INHD, QXO, ABVX, ASTC, BMNR, FIG, RXT, STI, STRC, WOLF) have backfill day-over-day jumps
>3×. KLAC is the confirmed ARTIFACT (10×-deflated backfill; Alpaca ground-truth ~2429); the other 10 are
mostly REAL moves / reverse-splits. BLAST RADIUS: MOMENTUM features only (mom_1d..mom_10d_rel — the only
ones spanning multi-day daily_closes across a step), ≤11 names on isolated dates (~0.03% of momentum
cells). The 13 intraday features are within-session self-consistent; fwd_30m/fwd_60m labels are
within-session; overnight labels come from the separate one-shot SPLIT-adjusted daily fetch — all
unaffected. Tasks #17 (re-fetch) / #18 (CA feed) handle the fix post-close.

SENSITIVITY PASS: re-ran the full battery EXCLUDING all 11 (conservative — over-excludes, since only
KLAC is a true artifact). Dropped 26,698 / 2,341 rows (30m / overnight). HEADLINE NUMBERS vs the full run:

  config                full-panel              excl-11                 moved?
  30m raw      IC 0.0270 bkeven 1.42bps   IC 0.0266 bkeven 1.41bps   no (rounding)
  30m rank     IC 0.0318 bkeven 1.44bps   IC 0.0322 bkeven 1.43bps   no
  30m vol      IC 0.0268 bkeven 1.37bps   IC 0.0269 bkeven 1.39bps   no
  30m lrank    IC 0.0010 bkeven 0.58bps   IC 0.0012 bkeven 0.64bps   no
  overnight raw   SURV-OUT sharpe -1.79      SURV-OUT sharpe -1.85      no (still neg)
  overnight rank  SURV-OUT sharpe -1.20      SURV-OUT sharpe -1.12      no
  overnight vol   SURV-OUT sharpe -1.68      SURV-OUT sharpe -1.80      no
  overnight lrank SURV-OUT sharpe -0.35      SURV-OUT sharpe -0.47      no (still neg = survivorship)

RESULT: ALL 8 configs => NO edge in BOTH runs. 30m IC ~0.027 / breakeven ~1.4bps and every overnight
survivorship-neutralized sharpe stays NEGATIVE — the discontinuity does not move the verdict. As
predicted (0.03% of momentum cells, intraday + labels untouched): "shown stable," not just "reasoned
stable." VERDICT STANDS with this documented caveat; the price-only "no tradeable edge" conclusion is
robust to the 11 flagged names. (Sensitivity output: experiments/battery_excl11.jsonl, gitignored.)

## ★ EXPLORATION ENGINE RESTART (Modeller, 2026-06-12 post-M1) — Ben's directive: always-running, refilled

M1 is DONE; the price-only verdict is the price-only ENDPOINT. Per Ben's directive the always-on
exploration engine (the experimenter service) had gone cold (queue 100% drained, all 19 ids in
results.jsonl) and is restarted TODAY with a curiosity-driven refill against the CLEAN v1.1.1 panel.

WHAT THE ENGINE DOES (so the next wake doesn't re-derive it): experimenter reads experiments/queue.json,
runs each id not already in results.jsonl via quantlib.research.run_experiment, appends to
results.jsonl + this file. run_experiment returns within-ts rank-IC, NW_t, shuffle canary, a
net-of-cost L/S backtest (net/sharpe/breakeven/turnover) AND gain importances. It is the IC-LEVEL
interrogation engine; the 4-gate survivorship/cost battery (experiments/battery.py) stays a manual
run for verdicts. The experimenter env is FEATURE_SET_VERSION=v1.0.0, so EVERY clean-panel entry MUST
set "set_version": "v1.1.1" explicitly (else it reads the stale v1.0.0 18-feat panel).

v1.1.1 FEATURE VOCABULARY (21): 11 price/calendar [ret_5m,ret_15m,ret_30m,ret_60m,vol_30m,vol_60m,
vol_z_30,vwap_dev,range_pct,gap_from_open,rel_ret_30m,minute_of_day,day_of_week] + 10 momentum
[mom_1d/3d/5d/10d and _rel variants]. NOTE: v1.1.1 has NO microstructure cols -> "nomicro"=="all";
"nocalendar" drops the 2 calendar feats -> 19 price+mom feats (the "price-only" battery config).

HARNESS EXTENSION (services/experimenter/main.py, this wake): the queue "features" field now also
accepts an explicit KEEP-list (JSON list of names, or "keep:a,b,c") and a "drop:a,b,c" form — on top
of all/nomicro/nocalendar. This unlocks single-feature interrogation, leave-one-out, and feature-group
isolation WITHOUT touching run_experiment. (resolve_feature_idx; backward-compatible.)

QUIET-WINDOW GUARD (same file + compose EXP_HEAVY_AFTER_PT=15:30): each experiment loads the full
~5.5M-row panel = heavy DB reads. The engine now DEFERS heavy reads until ≥15:30 PT on weekdays
(weekends always open) so the panel-wide reads grind OVERNIGHT and never contend with the live session
or prod's 13:00-15:00 PT post-close batch. Verified firing: "quiet window (09:49 PT < 15:30 PT) —
deferring". Engine rebuilt+restarted on the fresh image (stale-image hygiene). It will pick up the 48
new ids after 15:30 PT tonight.

QUEUE REFILL — 48 new C11_* / LONGSHOT_C11_* ids on v1.1.1 (curiosity-driven, run far more than we'd
ship). Organized as interrogations of WHY the clean 30m signal is real-but-uneconomic and WHERE any
residual might hide:
- BASELINES: C11_30m_raw_nocal / rank_nocal / raw_all (re-anchor on clean panel; quantify calendar crutch).
- PER-FEATURE SOLO (21 ids C11_solo_*): each feature ALONE at 30m raw -> which columns carry within-ts
  IC vs dead weight. The core "why is this feature weak" interrogation.
- MOMENTUM ISOLATION: mom_all / mom_abs / mom_rel / mom_short(1d) / mom_long(10d) — abs vs rel, short vs
  long lookback. Plus C11_price_only_30m (drop all momentum) to isolate intraday-price contribution.
- HORIZON CROSS: mom_60m, 60m_raw_nocal; overnight raw/rank/lambdarank/mom_rel (momentum should matter
  most overnight — where the battery found survivorship; this is the IC-level read).
- LEAVE-ONE-OUT (8 ids C11_loo_*): momentum minus each feature -> marginal contribution.
- VOL-SCALED LABEL: 30m + overnight vol_scaled nocal (surface alpha hidden under vol-ranking).
- LONG-SHOTS (4, deliberately weird, expect failure): reversal_short (ret_5m+ret_15m rank),
  range_vwap (intraday positioning), mom_vol_interaction (momentum conditioned on vol regime — GBM
  captures the interaction), mom_only_lambdarank (LTR on pure momentum).
RESUMED CADENCE: 2-4 deliberate long-shots/day continue (the 4 LONGSHOT_C11_* seed tonight's batch);
keep seeding ~daily, all logged here regardless of outcome (failures are data).

### NEW FEATURE IDEAS — registered for data collection (idea -> data -> experiment -> keep/discard)

Logged NOW because data collection has lead time (Ben's directive). These are NOT in the queue yet —
they need data the panel doesn't have. Coordinate storage with prod-architect-2 in the shared
featurestore path, parity with QA. Ranked by plausibility-of-edge at our latency:

1. ⭐ ORDER-FLOW IMBALANCE (OFI) — ALREADY THE PRIMARY M2 BET, not new, but the #1 data dependency.
   v1.2.0 OFI features (ofi_5m/15m/30m, signed_vol_z_30) need task #10 (build v1.2.0 panel over the
   order-flow names) + M2 50->500 scaling + the ≥15:50 ET close-exclusion (spec'd). Pilot ~6/26.
   This is where I most expect real edge; everything below is secondary.
2. INTRADAY REVERSAL/AUTOCORRELATION STRUCTURE — short-horizon mean-reversion is the classic
   microcap/midcap intraday edge. Partly testable NOW (LONGSHOT_C11_reversal_short), but the real
   version needs finer-grained intra-bar returns / a reversal-specific label (e.g. residual vs a
   short-window VWAP). DATA: derived from existing bars — LOW lead time. Spec a reversal label next wake.
3. CROSS-SECTIONAL DISPERSION / BETA-TO-UNIVERSE — each name's sensitivity to the universe move and
   its idiosyncratic residual. DATA: computable from the existing panel (rolling regression of name
   return on universe-median return) — derived feature, no new ingestion. Candidate for a v1.3.0
   feature group; spec next wake.
4. NEWS / EVENT FLAGS — a news stream table was scoped long ago (STATE.md "News stream -> news table",
   lower priority) but NEVER built. An is_news_today / minutes_since_news flag could gate or condition
   the ranking. DATA: needs the news ingestion built first (medium lead time; FMP key exists). Raise
   with Manager whether to prioritize the news stream now that price-only is exhausted.
5. SECTOR / INDUSTRY NEUTRALIZATION — demeaning momentum within GICS sector could clean the
   cross-sectional momentum signal (remove sector beta). DATA: needs a GICS sector map (STATE.md flags
   "sector needs a non-Alpaca source, e.g. the existing FMP key"). Medium lead time. Sector-relative
   momentum is a well-known improvement over raw relative momentum — worth the data.
6. BORROW / SHORTABILITY / HARD-TO-BORROW as a feature (not just a trade filter) — asset_metadata
   already has shortable/borrow flags refreshed daily; HTB names often carry short-side alpha. DATA:
   EXISTS (asset_metadata) — LOW lead time. Easy add once we snapshot it PIT into the panel.

NEXT-WAKE TODO (Modeller): (a) read tonight's C11_* results, write the "why is the signal weak"
synthesis (which solo features carry IC; does momentum isolation beat price-only; overnight IC-level
read); (b) seed the next 2-4 long-shots; (c) spec the lowest-lead-time new features (#3 dispersion/beta,
#6 borrow, #2 reversal label) as concrete featurestore additions for prod-architect-2; (d) chase the
OFI pilot prerequisites (#10 v1.2.0 panel, at-scale parity).

## PLAN-B DATA SPECS (Modeller, 2026-06-12) — #20 GICS sector map + #21 news-flag scoping

Manager ratified the "single-threaded on OFI" concern and created two tasks; I own requirements,
prod-architect-2 owns acquisition/feasibility. Specs below (also sent to prod). Neither needs heavy
DB reads, so written mid-session without touching the post-close batch window.

### #20 — GICS sector map (requirements for prod's FMP fetch)

PURPOSE: sector-neutral momentum. Raw relative momentum (mom_*_rel) demeans each name vs the WHOLE
universe; but the dominant cross-sectional factor on any given day is usually SECTOR (energy up, tech
down). Demeaning momentum within GICS sector removes that factor and should leave cleaner
idiosyncratic momentum — a well-known improvement. Also powers future dispersion/beta features.

FIELDS I NEED (per symbol):
- symbol (text, PK)
- gics_sector (text) — the 11 GICS sectors (the level I'll demean within). REQUIRED.
- gics_industry (text, nullable) — finer granularity for later; nice-to-have, not blocking.
- updated_at (timestamptz default now()).
STORAGE SHAPE: follow asset_metadata exactly — symbol-keyed, slowly-changing, ONE row/symbol,
refreshed periodically (sector rarely changes; a daily/weekly refresh in the scheduler is fine).
A new table `asset_sector` (symbol PK + updated_at) OR add gics_sector/gics_industry columns to
asset_metadata — prod's call; I just need to JOIN on symbol at panel-build time.
COVERAGE GATE (QA): NaN/null sector rate < 5% over the live universe. Names FMP can't map (ADRs,
recent listings) -> sector NULL is acceptable; I'll treat NULL-sector names as their own "UNKNOWN"
bucket for demeaning (don't drop them). Flag if coverage is materially worse than 95%.
WHAT IT CHANGES: I add a v1.3.0-candidate feature group (sector-demeaned momentum: mom_Xd minus the
within-sector-within-timestamp mean). NOT in the queue yet — needs this table first. This is the FAST
win that improves momentum REGARDLESS of OFI's fate.

FINALIZED CONTRACT (prod committed b856aa7, then renamed): the live table is
`sector_map(symbol PK, sector, industry, source, updated_at)` — NOT asset_sector, and the columns are
plain `sector`/`industry` (prod dropped the gics_ prefix since FMP's taxonomy isn't strict GICS — same
categorical grouping I asked for, cleaner names). My v1.3.0 join targets sector_map.sector. Fetch is
DEFERRED post-batch + gated on the FMP key landing in quant .env (flagged to Ben). At populate-time prod
pings me (a) null-sector rate vs the <5% gate AND (b) the DISTINCT sector-label SET so I can eyeball ~11
coherent buckets and catch FMP "N/A"/""/fragmented-30-bucket failure BEFORE building the feature; the
fetcher also snapshots the distinct-label set each refresh so QA can alarm on label drift.

### #21 — news/event-flag data: requirements memo (what would change a verdict)

THE GAP: every honest signal so far is price-derived and uneconomic; the whole edge bet is OFI. News/
event flags are an INDEPENDENT signal class (not price, not order-flow) — the Plan B that de-risks the
single-thread. This is SCOPING ONLY (memo, not a build) — prod scopes sources/latency/cost; I define
what the data must support to be worth collecting.

REQUIREMENTS (what the data must let me build/test):
1. EVENT TABLE, PIT-CORRECT: per (symbol, event_ts) — the headline/event timestamp must be the
   REAL publication time (not ingestion time), so features are point-in-time honest in backtest.
   Minimum columns: symbol, event_ts (timestamptz, the PIT anchor), event_type (text: news/earnings/
   filing/etc.), source (text). A headline string is nice for inspection, not required for features.
2. FEATURES IT WOULD POWER (the verdict-movers):
   - is_news_today / is_event_today (binary flag at the cadence ts) — does the ranking behave
     differently on event vs non-event names?
   - minutes_since_last_news (continuous) — recency of information.
   - news_burst_intensity (count of headlines in a trailing window, z-scored cross-sectionally) —
     attention spikes; the most plausible alpha-bearing one.
   These are GATING/CONDITIONING features (interact with momentum/reversal) more than standalone
   rankers — GBM can split on them. I'd test them as additions to the existing feature set AND as a
   regime split (IC on event-days vs non-event-days).
3. WHAT WOULD CHANGE A VERDICT (the bar for "worth building"): the memo should let me judge whether
   the data could plausibly produce a feature that (a) has non-trivial within-ts IC on its own OR (b)
   materially sharpens momentum/reversal IC when interacted — under the SAME 4-gate battery (net-of-
   cost, canary, survivorship). If coverage is too sparse (e.g. only mega-caps get news) or latency
   too poor (events stamped at ingestion not publication), it CAN'T be PIT-honest -> not worth it.
SOURCE CANDIDATES (prod compares): Alpaca news API (our broker, free, websocket+historical — FIRST
candidate); FMP news endpoints (key exists); EDGAR filings feed (structured, event-typed, but filings-
only). Compare on: PIT-correct publication timestamps, symbol coverage across the ~1000 universe (not
just mega-caps), historical depth (need ~600 days to backtest on our panel), latency, cost.
MEMO DELIVERABLE (prod): source comparison table on those axes + recommended source + storage shape +
lead time to first experiment. Then Manager decides whether/when to build. Sequence: AFTER M2 capture
scaling is underway — do not preempt M2.

## REVIEW POLICY ADOPTED (Modeller, 2026-06-12) — role authorship + tiering of my lane

Ben's binding REVIEW_POLICY.md (commit e13ead7): commit author = ROLE (`--author="modeller
<modeller@quant-team>"`), WHY lives here in EXPERIMENTS.md, and PRODUCTION feature/label/training
changes are Tier 1 (role branch + PR + mapped reviewer). My lane specifics:
- experiments/ (queue, battery, results) = TIER 2 sandbox: direct-commit to master, friction-free —
  this is most of my work and the exploration mandate depends on it staying unblocked.
- quantlib feature/label DEFINITIONS, model-server/training path = TIER 1: when I propose a real
  feature group (e.g. the v1.3.0 sector-demeaned momentum, or any OFI feature definition) for
  PRODUCTION, it goes on a `modeller/<topic>` branch → PR → mapped reviewer (qa for data-semantics,
  prod for runtime) → Manager merges. The IC-level exploration that motivates it stays Tier 2.
- I am the named REVIEWER (adversarial) on any peer PR touching feature/label/training definitions.

SELF-FLAG (honesty, not buried): my commit 1012d2a touched services/experimenter/main.py +
docker-compose.yml — both Tier 1 under this policy (service runtime + compose). It PREDATES the policy
(e13ead7 was committed after), so not a violation, but the experimenter is a real service and the
quiet-window guard / resolve_feature_idx changes are now LIVE without a prod adversarial review.
Flagging to the Manager for prod-architect's RETROACTIVE review; future experimenter/compose edits go
through a PR. (The change is low-risk — isolated sandbox service, no order/data-corruption path — but
the policy maps it to prod regardless and I won't self-exempt.)

## ★ WEEKEND FEATURE-EXPLORATION SPRINT (Modeller, 2026-06-12) — queue + NEW feature families

Ben directive: weekend = uninterrupted feature-exploration sprint (experimenter + 3090, zero RTH
constraints Sat/Sun). Two deliverables logged here: (A) the deep weekend queue, (B) 3 NEW feature
families spec'd for collection. Data inventory verified live before proposing (grounded, not guessed):
- bars_1m source='backfill': 253M rows / 1213 syms / back to 2023-12-01 (~2.5yr) — the momentum + CA
  feature source; deep enough for everything below.
- quote_agg_1m: mean/median_spread_bps, mean_bid/ask_size, quote_imbalance, n_quotes — but only 52
  names / ~2.5 days (same live-stream subset as OFI -> same M2 scaling gate, plumbing-grade now).
- asset_metadata: shortable/easy_to_borrow/marginable/fractionable — NO float/market-cap (needs FMP).
- news table EXISTS (empty): id, created_at (publication anchor), headline, summary, source, url,
  symbols[] — the #21 landing table is already scaffolded.
- corporate_actions: NOT yet (lands tonight, #18 — splits AND dividends). sector_map: DDL committed,
  not yet populated (#20, post-batch+key).

### (A) WEEKEND QUEUE — 134 new W11_*/LONGSHOT_W11_* on v1.1.1 (total queue now 201; ~182 unrun)

At ~2-5 min/experiment on the 5.5M-row panel this is ~6-15h of grind — fills Fri-night into the
weekend; the engine re-reads the queue each cycle so I can deepen it. Families (all set_version v1.1.1):
- grid (12): full horizon×label sweep on price-only (nocalendar) — systematic baseline map.
- grp (36): feature-GROUP isolation (momentum/momrel/momabs/ret/vol/positioning) × 3 horizons × {raw,
  rank} — which FAMILY carries within-ts signal where.
- solo (16): momentum single-features at 60m + overnight (extends C11's 30m solos to longer horizons).
- pair (16): momentum term-structure (adjacent-lookback pairs) at 30m + overnight.
- int (21): INTERACTION probes — momentum×vol, momentum×ret, momentum×positioning, ret×vol, etc.
  (GBM splits on the cross-term; does conditioning sharpen IC vs either group alone?) × 3 horizons.
- lab (8): label sweep on momentum-only & momrel-only at overnight (lowest-turnover horizon).
- loo19 (19): leave-one-out on the full price-only set — each feature's marginal contribution.
- LONGSHOT (6): gap-only-overnight, vol-as-signal, range-overnight-reversal, kitchen-sink-lambdarank-
  60m, momrel-volscaled-60m, ret_60m→overnight carryover. Deliberately weird, expect failure, logged.
DISCIPLINE: all Tier-2 sandbox (experiments/); IC-level exploration, NOT edge claims — verdicts still
need the 4-gate battery. No accidental "early reads" promoted to belief.

### (B) THREE NEW FEATURE FAMILIES — spec'd for collection (idea→data→experiment→keep/discard)

Beyond sector (#20) and news (#21). Ranked by lead-time-to-first-experiment (Ben wants NEW collection
spec'd TODAY so prod can start Monday open, or tonight if API-historical):

**FAMILY A — EX-DIVIDEND / CORPORATE-ACTION BEHAVIOR (lowest lead time — CA feed lands TONIGHT).**
- HYPOTHESIS: names going ex-dividend have mechanical overnight price drops (~the dividend amount) and
  documented ex-div-day return anomalies (dividend-capture flows, reinvestment). An is_ex_div_today /
  days_to_ex_div / div_yield flag could (a) clean the overnight label (the ex-div drop is not alpha —
  it's a known mechanical move we may want to NEUTRALIZE, like survivorship) and (b) be a conditioning
  feature for overnight ranking. Splits similarly: days_since_split for post-split drift.
- DATA SOURCE: the #18 corporate_actions table (Alpaca CA API, splits+dividends) landing tonight —
  API-HISTORICAL, so backfillable over the ~2.5yr panel immediately, no waiting for live collection.
- COLLECTION COST: near-zero incremental — #18 already fetches it tonight for adjustment-gating; I just
  need it exposed as a PIT (symbol, ex_date, type, amount) lookup joinable at feature-compute time.
- PARITY PLAN: ex-dates are calendar facts (no live/backfill skew risk); the only PIT discipline is
  "known as-of the cadence ts" — use ex_date strictly, announcement_date if available for anticipation.
  Cheapest, most defensible new family; mostly a LABEL-HYGIENE win (neutralize the mechanical ex-div
  overnight drop) even if it's not standalone alpha.

**FAMILY B — DISPERSION / BETA-TO-UNIVERSE (ZERO new collection — pure derived from existing bars).**
- HYPOTHESIS: each name's beta to the universe move and its idiosyncratic residual are distinct from
  raw momentum. Low-beta/high-idio-residual names may carry cleaner cross-sectional signal; the
  residual-from-universe return is the "alpha" component that momentum conflates with market beta.
  Also: realized cross-sectional DISPERSION as a regime feature (high-dispersion days = more rankable).
- DATA SOURCE: NONE NEW — computed from bars_1m backfill (rolling regression of name return on
  universe-median return -> beta + residual; dispersion = cross-sectional std of returns per ts).
- COLLECTION COST: zero. It's a featurestore computation (like momentum). Lead time = my dev time to
  add the feature group, not data acquisition. Candidate v1.3.0/v1.4.0 group.
- PARITY PLAN: same as existing price features (derived from the same backfill bars that are already
  parity-validated; live==backfill holds because it's the same quantlib computation). The ONLY new
  discipline: the universe-median must be computed PIT over the same membership the labels use (already
  solved for labels — reuse that path). I can prototype this in experiments/ THIS WEEKEND as a derived
  column without prod, then propose the production feature group via Tier-1 PR if it shows promise.

**FAMILY C — QUOTE / NBBO MICROSTRUCTURE (data collects now, but M2-gated like OFI).**
- HYPOTHESIS: spread, quote_imbalance (bid vs ask size), and quote intensity are microstructure
  signals distinct from trade-based OFI — quote imbalance can lead trades. spread_bps as a liquidity/
  cost proxy doubles as a per-name cost-model input (improves the battery's net-of-cost gate).
- DATA SOURCE: quote_agg_1m ALREADY COLLECTS (mean/median_spread_bps, bid/ask sizes, quote_imbalance,
  n_quotes) — but only the 52 order-flow names, ~2.5 days.
- COLLECTION COST: zero NEW (already streaming) BUT same M2 scaling gate as OFI — pilot-grade only
  after 50->500 + at-scale parity (#15). Bundle quote features INTO the v1.2.0/OFI panel build (#10)
  so they ride the same scaling, rather than a separate pipeline.
- PARITY PLAN: identical to OFI — settled-day quote-agg parity at scale (QA), close-minute exclusion
  (≥15:50 ET, same as OFI). Folds into the OFI pilot discipline; NOT a separate weekend item.

RECOMMENDATION TO MANAGER: prioritize FAMILY A (ex-div, free once #18 lands tonight, immediate label-
hygiene value) and FAMILY B (dispersion/beta, zero collection, I can prototype this weekend). FAMILY C
rides the OFI pipeline — no separate spend. Float/market-cap (FMP, like sector) is a possible FAMILY D
later but lower priority than these three.

### (C) OFI PIPELINE VALIDATION (weekend, plumbing-grade ONLY — no early reads)
Prod builds the v1.2.0 OFI panel over 52 names TONIGHT (~2.5 days data = PLUMBING-GRADE, NOT pilot).
Weekend goal: validate the OFI experiment pipeline END-TO-END (panel loads, 4 gates run, cost model
applies) so the real trigger-gated pilot (~6/26) has ZERO pipeline risk. I will run v1.2.0 experiments
LABELED "PIPELINE-VALIDATION — NOT A READ" and record any IC as plumbing-status only; the ~2.5-day
sample is far too thin for ANY belief. Pilot discipline (≥10 full-session 50-name days from the
confirmed-capture clock + the ≥15:50 exclusion + at-scale parity prerequisite) stands unchanged.

## FAMILY A — EX-DIV/CA PIT-LOOKUP SPEC (Modeller → prod #18, 2026-06-12, Manager-prioritized)

Manager approved Family A and routed me to spec prod the PIT lookup (like #20). The #18 fetcher already
parses cash_dividends + splits and the corporate_actions table populates tonight; this spec makes the
consumer side PIT-correct BY CONSTRUCTION. Primary value = LABEL HYGIENE (neutralize the mechanical
ex-div overnight drop — same honesty class as survivorship demean), secondarily a conditioning feature.

REQUIREMENTS (what I need exposed; prod owns the table from #18):
- A PIT lookup keyed (symbol, ex_date) — the actions table from #18, or a view over it. Columns I
  consume: symbol, ex_date (date), action_type ('cash_dividend'|'split'|...), cash_amount (per-share $,
  null for splits), split_ratio (null for dividends). announcement_date if Alpaca provides it (lets me
  build days_to_ex_div anticipation features; nice-to-have, not blocking).
- PIT DISCIPLINE: at a feature-compute cadence ts, only actions with announcement_date <= ts (or
  ex_date <= ts for realized flags) are visible. ex_date is a calendar fact so no live/backfill skew —
  the only honesty rule is "don't see a dividend before it's announced/ex." Join at feature-compute
  time (same pattern as sector_map), NOT baked into feature_vectors.
FEATURES IT POWERS: is_ex_div_today (binary at ts), days_to_next_ex_div, days_since_ex_div,
trailing_div_yield (sum trailing-12m cash_amount / price). LABEL-HYGIENE USE (the priority): an
ex_div_adjustment to the OVERNIGHT label so the mechanical close→next-open drop on ex-date is removed
before demeaning — tests overnight momentum free of the dividend artifact.
COVERAGE/PARITY: dividends are sparse (most names most days = no action) so "coverage" = did we fetch
the full 2.5yr of actions for the panel universe; QA check = action counts per month are non-zero and
stable (a month with 0 dividends = a fetch gap). API-historical so backfillable immediately tonight.

### FAMILY A FINALIZED — corporate_actions_pit VIEW (prod staged to spec, 2026-06-12)

Prod staged the PIT view exactly to spec (db/init/05_corporate_actions.sql, CREATE OR REPLACE over the
#18 table). My consuming code targets the VIEW `corporate_actions_pit`, columns:
  (symbol, ex_date, action_type, cash_amount, split_ratio, announcement_date, record_date, payable_date)
- action_type normalized to my taxonomy: cash_dividends→'cash_dividend'; *_splits→'split'.
- cash_amount = per-share $ (NULL splits); split_ratio = new/old forward factor (NULL dividends).
- announcement_date = COALESCE(declaration_date, process_date)::date, NULL if absent → I fall back to
  ex_date for realized flags. Prod verifies the real Alpaca field at populate-time tonight and may
  CREATE OR REPLACE the view (so days_to_ex_div anticipation gets a real announcement_date if available).
PIT discipline is mine by construction: JOIN at feature-compute time (like sector_map), reveal only
announcement_date<=ts (anticipation) / ex_date<=ts (realized). Live rows land tonight from #18's fetch
(dividends specifically verified, not just splits, per Ben). Prod pings when queryable; QA owns the
2.5yr-no-zero-month coverage check. I prototype the overnight-label ex-div hygiene adjustment
post-weekend-read/#16 per my clock. Zero extra collection.
| 2026-06-12T19:17:27+00:00 | C11_30m_raw_nocal | fwd_30m | raw | 19 | 4840765 | 0.02698 | 19.988 | -0.00175 | Clean v1.1.1 30m baseline: raw, no-calendar (19 feats). Anchor the per-feature interrogation; expect IC ~0.027 net-negative matching battery. |
| 2026-06-12T19:20:13+00:00 | C11_30m_rank_nocal | fwd_30m | rank | 19 | 4840765 | 0.03179 | 21.404 | -0.00083 | Clean v1.1.1 30m: rank label, no-calendar. Trading-aligned loss; does rank beat raw IC on clean data? |

## ★ #16 STAGING REVIEW — PASS (Modeller, 2026-06-12, fired early per Ben's DO-IT-NOW)

Fired the #16 v1.1.1 staging train NOW (not post-close) per Ben's DO-IT-NOW directive. CAUGHT A
LIVE-PATH HAZARD FIRST: the built trainer image was STALE (14h, predated the MODEL_FILENAME override
4b6b7fe) — it hard-coded output to model_fwd_30m.txt, the LIVE model-server path. Training v1.1.1 on
that stale image would have written a 21-feature file over the live 18-feature model and BROKEN live
scoring on the model-server's next reload. Rebuilt the trainer image, diff-verified it matches source
(MODEL_FILENAME present), THEN trained. (This is exactly the stale-image bug class that hit the team 3x;
the diff-before-run check caught it.)

STAGING TRAIN RESULT (MODEL_FILENAME=model_fwd_30m_v1.1.1.txt, FEATURE_SET_VERSION=v1.1.1):
  panel: 4,840,765 rows / 21 feats / 7347 timestamps / set=v1.1.1 / fwd_30m
  REAL  : mean rank-IC = 0.0266   NW t = 19.53   (6123 test ts)
  CANARY: mean rank-IC = 0.0004   (clean ~0 — no leakage)
  saved model_fwd_30m_v1.1.1.txt + .meta.json (21 features) to /models

REVIEW VERDICT = PASS (against my pre-registered bar ~0.027 IC, clean canary, materially-higher=red):
- IC 0.0266 is RIGHT ON the battery scorecard (30m raw IC 0.027) — NOT materially higher, no leakage
  red flag. Cross-validates the experimenter's concurrent C11_30m_raw_nocal (IC 0.02698, canary
  -0.00175) — independent code path (trainer 5-fold vs experimenter), same number => trustworthy.
- CANARY 0.0004 ≈ 0 — clean, no train/serve leakage in the v1.1.1 contract.
- LIVE MODEL UNTOUCHED (verified): models/model_fwd_30m.txt still Jun-10 14:03; staging file is the
  new Jun-12 file. MODEL_FILENAME override worked as designed — zero live-path risk.
- This is a NO-EDGE HYGIENE model (0.0266 IC is net-negative after cost per the battery — not edge).
  It is NOT promoted to live: the deliberate 18→21 contract upgrade waits for a model WORTH serving
  (v1.2.0/OFI post-M2) with three-way deploy sign-off. The staging artifact exists for provenance +
  to prove the clean trainer path produces the expected model. #16 (train + review) DONE.
| 2026-06-12T19:22:40+00:00 | C11_30m_raw_all | fwd_30m | raw | 21 | 4840765 | 0.02678 | 19.558 | -0.00292 | Clean v1.1.1 30m raw, ALL 21 incl calendar. Quantify how much of any IC is the within-ts-constant calendar crutch (should add ~0 within-ts). |
| 2026-06-12T19:24:48+00:00 | C11_solo_ret_5m | fwd_30m | raw | 1 | 4840765 | 0.01056 | 8.146 | 0.00111 | Single-feature interrogation: ret_5m ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |

## FAMILY B PROTOTYPE — dispersion/beta/idiosyncratic-residual (Modeller, 2026-06-12, DO-IT-NOW)

Started the Family B prototype NOW (zero data dependency, so no reason to defer). Script:
experiments/family_b_dispersion.py (Tier-2 sandbox). Derives 4 NEW features PURELY from the existing
v1.1.1 panel's return columns — no panel rebuild, no new collection:
- univ_beta: each name's return-term-structure [ret_5m,15m,30m,60m] regressed on the cross-sectional
  MEAN term-structure (its sensitivity to the common/market move), estimated WITHIN each snapshot.
- idio_resid_30m / _60m: ret_30m/60m minus univ_beta * universe_ret — the IDIOSYNCRATIC return
  (the "alpha" component raw momentum conflates with market beta).
- dispersion_30m: cross-sectional std of ret_30m per ts — a regime feature (constant within ts, so
  it can only act through interactions, like calendar).
All strictly within-timestamp => point-in-time honest. Runs the SAME 4 battery gates (IC vs raw return
+ shuffle canary + net-of-cost L/S + survivorship demean) on three variants at 30m + overnight:
baseline_price_only (19 feats) vs plus_family_b (23) vs family_b_only (4).
HYPOTHESIS / WHAT WOULD CHANGE A VERDICT: if +family_b lifts IC ABOVE the canary AND improves
breakeven vs baseline, it's worth a real feature group (proposed via Tier-1 PR). If it moves nothing,
that SHARPENS the "data-starved, not model-starved" read — the one genuinely-new FREE signal this
weekend showing nothing is strong evidence the price panel is exhausted and only new DATA (OFI, news,
ex-div) can help. Honest either way; this is a prototype, NOT an edge claim.
NOTE on the beta proxy: a true beta needs a trailing time series; this within-snapshot 4-horizon-vector
beta is a cheap PROXY computable from the panel alone. If the proxy shows promise, the production
version would use rolling per-name regression on the bar history (a real featurestore computation) —
but the proxy is the cheap weekend read on whether the idea has ANY legs before investing in that.
| 2026-06-12T19:27:14+00:00 | C11_solo_ret_15m | fwd_30m | raw | 1 | 4840765 | 0.00428 | 4.476 | 0.00069 | Single-feature interrogation: ret_15m ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |

### FAMILY B SMOKE RESULT (120-day, 2026-06-12) — no meaningful lift; full run pending

experiments/family_b_smoke.jsonl (gitignored). 120-day sample, 3 variants × 2 horizons:

  variant               horizon    nf      IC     canary  breakeven  surv_sharpe
  baseline_price_only   fwd_30m    19  +0.01510  -0.00923   0.98bps    -4.92
  plus_family_b         fwd_30m    23  +0.01602  -0.00989   0.93bps    -4.03
  family_b_only         fwd_30m     4  +0.00257  -0.00087   0.01bps    -7.32
  baseline_price_only   overnight  19  -0.00179  +0.00230   3.94bps    -0.43
  plus_family_b         overnight  23  -0.00033  +0.00016   2.54bps    -1.89
  family_b_only         overnight   4  +0.00488  -0.00118  -0.42bps    -2.51

READ (smoke-grade, honest): Family B does NOT lift the signal above the canary economically.
- 30m: +family_b IC 0.0151→0.0160 (+0.0009) but the canary rose proportionally (-0.0092→-0.0099) —
  the lift is WITHIN canary noise, not real. family_b_only IC 0.0026 ≈ its canary. Survivorship sharpe
  stays deeply negative.
- overnight: the only faintly-interesting cell is family_b_only IC +0.0049 (canary -0.0012) — the
  idiosyncratic RESIDUAL alone shows a small standalone overnight IC above its canary. BUT breakeven
  -0.42bps (loses money immediately) and survivorship sharpe -2.51 — uneconomic + survivorship-driven.
VERDICT (preliminary, pending full panel): the idiosyncratic-residual idea has the faintest pulse at
overnight but fails cost + survivorship; dispersion/beta add nothing at 30m. CONSISTENT WITH and
SHARPENS "data-starved, not model-starved" — the one genuinely-new FREE signal shows ~nothing.
Full-panel run launched (the firm read); 120d is directional only. If the full panel confirms, Family B
is a discard (logged, not shipped) and the case for NEW DATA (OFI/news/ex-div) as the only path
strengthens. NOTE: the within-snapshot beta is a cheap proxy; a true rolling-regression beta on bar
history could differ — but a proxy showing zero is weak evidence FOR investing in the expensive version.
| 2026-06-12T19:29:13+00:00 | C11_solo_ret_30m | fwd_30m | raw | 1 | 4840765 | -0.00073 | -0.947 | 1e-05 | Single-feature interrogation: ret_30m ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |

### EXPERIMENTER GUARD — Manager ruling: OPEN on evidence + prod pause-authority (2026-06-12)

The ≥15:30 guard is OPEN (EXP_HEAVY_AFTER_PT=00:00). Manager ruling (provenance-checked):
- The "start the queue NOW / batch-precaution is without evidence" relay did NOT come from the Manager
  (dual-manager mislabel again). The Manager's only directive kept the ≥15:30 guard. I surfaced the
  contradiction instead of silently obeying the louder one — correct per the provenance rule
  (un-board-reflected contradictory directives come back to the Manager).
- ON THE MERITS the open guard WINS on EVIDENCE: 25+ min of grind ran clean alongside live collection +
  the #16 train + prod's KLAC re-fetch (217k bars); DB unbothered. Remaining batch items are mostly
  restarts/image-builds (not DB-contention-sensitive); #12 backfill is separately rate-gated.
- STANDING CONDITION: prod-architect-2 has PAUSE AUTHORITY over the grind for the batch duration — if
  they report ANY contention, I re-close the guard (EXP_HEAVY_AFTER_PT=15:30 + restart, seconds) with
  NO round-trip to the Manager. I told prod they hold that authority.
LESSON (operating): the quiet-window guard was precaution; it's now empirically tested as unnecessary
under current load. Keep the guard CODE (cheap insurance for a real future reason) but default OPEN.
| 2026-06-12T19:31:41+00:00 | C11_solo_ret_60m | fwd_30m | raw | 1 | 4840765 | -0.00177 | -2.223 | -0.00165 | Single-feature interrogation: ret_60m ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |

## ★ FAMILY A — EX-DIV OVERNIGHT-LABEL ARTIFACT CONFIRMED + HYGIENE VALIDATED (Modeller, 2026-06-12)

Prod populated corporate_actions_pit LIVE (DO-IT-NOW): 7133 cash_dividends / 5565 in the panel window
(2024-01..2026-06) over 607 symbols. SCOPE CUT from prod's finding: Alpaca's CA feed has NO
announcement/declaration date (process_date is POST-ex, a settlement date) → REALIZED features + the
overnight-label HYGIENE fix work off ex_date (supported), but ANTICIPATION features (days_TO_ex_div,
pre-announcement) are NOT supportable from this source — they'd need FMP declaration dates or the #21
news feed. So days_to_ex_div is OFF the table for now; is_ex_div_today / days_since / trailing_yield /
the hygiene fix are ON.

DIAGNOSTIC (SQL, full panel) — the ex-div overnight artifact is REAL and exactly where theory predicts.
Overnight label = close(D)→open(D+1) excess-vs-universe-median. The mechanical ex-div drop hits the
label whose FORWARD open is the ex-morning (label_date+1 == ex_date):

  bucket                                   n        mean_overnight_label
  non-ex baseline                          420,635  +0.000474   (normal overnight drift)
  ex_date == label_date                    4,098    +0.000157   (forward open is post-ex; no effect ✓)
  ex_date == label_date+1 (fwd=ex-morning) 3,291    -0.005157   ← -51.6 bps mechanical DROP

HYGIENE VALIDATION (add back the dividend yield to the affected labels):
  mean ex-night label            -0.005157
  mean (-cash_amount/prior_close) -0.006103   (the dividend yield ≈ the drop)
  label + dividend_yield          +0.000946   ← back to ~baseline (+0.0005). ARTIFACT NEUTRALIZED.

READ: ~52bps of MECHANICAL, non-alpha negative return on ~3,291 (symbol, night) cells, ~85% explained
by the dividend yield. The overnight model could be (mis)learning this as "signal." This is a genuine
LABEL-HYGIENE win — same honesty class as survivorship demean. NEXT: re-run the overnight battery on
ex-div-CORRECTED labels (add yield back on affected nights) and see whether the survivorship/IC picture
changes — does removing the dividend artifact clean or kill the residual overnight signal? (Prototype
next; the diagnostic above already justifies the correction regardless of the battery outcome — a
known mechanical contaminant should not be in the label.)

### PROVENANCE DECISION — v1.1.1 stays FROZEN, KLAC fix carried by v1.1.2 (Modeller, 2026-06-12)

#17: prod re-fetched KLAC bars clean (max day-jump 10×→1.19×, consistent Adjustment.ALL). The #17 spec
said "recompute KLAC's v1.1.1 momentum cells"; prod surfaced it as a provenance call (my lane). DECISION
= (B) leave v1.1.1 frozen; do NOT recompute KLAC's v1.1.1 cells in-place.
WHY: v1.1.1 (5.5M rows / 06:43Z) is the EXACT pinned artifact my "NO EDGE" verdict was computed on — its
integrity as a reproducible reference outweighs the correctness of a few KLAC cells. Mutating it would
break the verdict↔panel mapping. And the KLAC caveat is already MOOT: the sensitivity pass (984e7fa)
excluded all 11 split-discontinuity names (KLAC incl.) and the verdict moved nothing — so the verdict
provably doesn't depend on KLAC's cells. Recomputing = pure provenance cost, zero verdict benefit.
GO-FORWARD: v1.1.2 (the #12B full-universe rebuild) is the next research panel; it inherits clean KLAC
+ the other 10 corrected names automatically from the now-fixed bars. COMMITMENT: when v1.1.2 lands,
re-run the battery on it to RE-CONFIRM "no edge" on fully un-caveated data (a v1.1.2 task). The ex-div
overnight-label hygiene (Family A) should also fold into the v1.1.2 label build so the go-forward panel
has clean overnight labels from the start.
| 2026-06-12T19:34:08+00:00 | C11_solo_vol_30m | fwd_30m | raw | 1 | 4840765 | -0.00124 | -1.357 | -0.00119 | Single-feature interrogation: vol_30m ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:36:22+00:00 | C11_solo_vol_60m | fwd_30m | raw | 1 | 4840765 | -0.00115 | -1.312 | -0.00037 | Single-feature interrogation: vol_60m ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |

### FAMILY B FULL-PANEL VERDICT (2026-06-12) — DISCARD (survivorship at overnight; nothing at 30m)

Full v1.1.1 panel (experiments/family_b_results.jsonl, gitignored):

  variant               horizon    nf      IC     canary  breakeven  surv_sharpe
  baseline_price_only   fwd_30m    19  +0.02698  -0.00175   1.42bps    -3.51
  plus_family_b         fwd_30m    23  +0.02666  -0.00233   1.42bps    -3.29
  family_b_only         fwd_30m     4  +0.00681  +0.00043   0.12bps    -6.57
  baseline_price_only   overnight  19  +0.01420  -0.00557   3.20bps    -1.79
  plus_family_b         overnight  23  +0.01244  -0.00555   2.79bps    -1.48
  family_b_only         overnight   4  +0.01930  -0.00013   2.45bps    -1.61

READ (honest, full panel):
- 30m: Family B adds NOTHING. +family_b 0.0267 vs baseline 0.0270 (a hair LOWER); family_b_only 0.0068
  ≈ weak. Confirms the smoke. The idiosyncratic-residual/dispersion features carry no 30m signal.
- overnight: the EYE-CATCHER — family_b_only IC 0.0193 (> the 19-feat baseline 0.0142), CLEAN canary
  (-0.0001), breakeven 2.45bps (CLEARS ~2bps cost). BUT survivorship-neutralized sharpe = -1.61 (deeply
  negative) => it is SURVIVORSHIP, not timing alpha. The idio-residual ranks persistent per-symbol drift
  (ex-post survivors), exactly like every other overnight "signal" we've found. Per-symbol demean kills it.
- VERDICT = DISCARD under the gates. Family B is the genuinely-NEW FREE signal this weekend and it fails
  — SHARPENS "data-starved, not model-starved." The price panel (+ derived combinations of it) is
  exhausted; only new DATA (OFI/news/ex-div) can move us.

⚠️ CAVEAT (why the ex-div corrected battery matters NOW): this overnight run uses the UNCORRECTED labels,
which contain the confirmed -52bps ex-div artifact. The overnight family_b_only "signal" could be PARTLY
the ex-div contamination (idio-residual on ex-div names = the dividend drop dressed as idiosyncratic
return). The ex-div-corrected overnight battery (experiments/exdiv_corrected_battery.py) tests exactly
this: does removing the dividend artifact change the overnight picture? INTERPRETATION of that is HELD
until qa-2 verifies the ex-div diagnostic. NOTE on the proxy: Family B's within-snapshot beta is a cheap
4-horizon-vector proxy; the discard is firm enough that the expensive rolling-regression beta is NOT
worth building (a proxy showing survivorship is weak evidence FOR the costly version).
| 2026-06-12T19:38:22+00:00 | C11_solo_vol_z_30 | fwd_30m | raw | 1 | 4840765 | -0.00029 | -0.517 | 2e-05 | Single-feature interrogation: vol_z_30 ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:22+00:00 | C11_solo_vwap_dev | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: vwap_dev ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:22+00:00 | C11_solo_range_pct | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: range_pct ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:22+00:00 | C11_solo_gap_from_open | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: gap_from_open ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:22+00:00 | C11_solo_rel_ret_30m | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: rel_ret_30m ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:22+00:00 | C11_solo_mom_1d | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: mom_1d ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:23+00:00 | C11_solo_mom_3d | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: mom_3d ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:23+00:00 | C11_solo_mom_5d | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: mom_5d ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:23+00:00 | C11_solo_mom_10d | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: mom_10d ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:23+00:00 | C11_solo_mom_1d_rel | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: mom_1d_rel ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:23+00:00 | C11_solo_mom_3d_rel | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: mom_3d_rel ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:23+00:00 | C11_solo_mom_5d_rel | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: mom_5d_rel ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:23+00:00 | C11_solo_mom_10d_rel | fwd_30m | raw |  |  |  |  |  | Single-feature interrogation: mom_10d_rel ALONE at 30m raw. Isolates this feature's standalone within-ts IC — find which carry signal vs dead weight. |
| 2026-06-12T19:38:23+00:00 | C11_mom_all_30m | fwd_30m | raw |  |  |  |  |  | Momentum-only (10 feats) at 30m raw. Standalone momentum IC vs price-only — does cross-sectional momentum carry the signal? |
| 2026-06-12T19:38:24+00:00 | C11_mom_abs_30m | fwd_30m | raw |  |  |  |  |  | Absolute momentum only (mom_1d..10d). Abs vs rel split: is raw price momentum or universe-relative momentum the carrier? |
| 2026-06-12T19:40:21+00:00 | C11_mom_rel_30m | fwd_30m | raw | 4 | 4840765 | -0.00336 | -2.161 | -0.0003 | Relative momentum only (mom_*_rel). The universe-demeaned momentum — should be the cleaner cross-sectional signal. |

## ★ OFI PIPELINE VALIDATION (Modeller, 2026-06-12) — PLUMBING PASS + one feature-def finding

⚠️ NOT A SIGNAL READ. v1.2.0 OFI panel (#10, prod): 1516 vectors / 50 names / 3 days — plumbing-grade.
Goal = prove the experiment pipeline ingests OFI E2E so the real pilot (~6/26) has zero pipeline risk.
Script: experiments/ofi_pipeline_validation.py (bypasses the min-rows verdict guard ON PURPOSE — this
is plumbing, not a verdict).

PLUMBING = PASS (all 3 asserts green):
1. load_panel(v1.2.0) returns the 25-feature vectors; OFI at positions 22-25 (ofi_5m/15m/30m,
   signed_vol_z_30) exactly as prod laid out. 816 fwd_30m rows / 50 syms / 18 ts; source='historical';
   v1.2.0 IS registered in feature_sets (25 names) — contra prod's note, the registry entry exists.
2. All 4 OFI features PRESENT + NON-DEGENERATE: ofi_5m/15m/30m ~640 uniq, range ~[-0.93,+0.93]
   (sane signed imbalances), NaN 21.6% (thin-window edges; matches prod's ~85% real).
3. run_experiment ingests all 25 feats incl OFI and completes — gates compute, no error. (IC -0.0396 /
   canary -0.0139 are PLUMBING NOISE on 3 days / n_test_ts=4 — NOT a read; recorded only to prove the
   harness runs.)
=> OFI experiment pipeline validated end-to-end. Pilot pipeline risk = 0.

⚠️ FEATURE-DEFINITION FINDING (flag to prod, real but not pipeline-blocking): signed_vol_z_30 is NOT
properly normalized. A feature named "_z" (z-score) should be std≈1, range≈[-5,+5]. ACTUAL: mean 5.86,
STD 141.49, range [-3158, +1234], median 0.10. It's a raw signed-volume quantity (or the rolling-std
denominator is broken — plausibly the 3-day window lacks the history to compute the normalization, so
it divides by ~0 or not at all). NOT fatal for GBM (tree splits are scale-invariant) but fragile +
misleadingly named; the ofi_5m/15m/30m features ARE correctly in [-1,1]. Worth fixing in the v1.2.0
feature definition before the pilot interprets signed_vol_z_30. Re-check once there's >10 days of
trade_agg history (the rolling z-window may simply need more data).
| 2026-06-12T19:42:20+00:00 | C11_mom_short_30m | fwd_30m | raw | 2 | 4840765 | -0.00074 | -0.62 | 0.00131 | Short-lookback momentum (mom_1d, mom_1d_rel) at 30m. Does recent (1d) momentum dominate? |
| 2026-06-12T19:44:18+00:00 | C11_mom_long_30m | fwd_30m | raw | 2 | 4840765 | 0.00033 | 0.307 | -2e-05 | Long-lookback momentum (mom_10d, mom_10d_rel) at 30m. Does the 10d lookback carry more than 1d? |
| 2026-06-12T19:46:15+00:00 | C11_price_only_30m | fwd_30m | raw | 13 | 4840765 | 0.02819 | 20.918 | -0.00248 | Price/calendar features WITHOUT momentum (drop all mom_*). Isolate the intraday-price contribution alone on clean data. |
| 2026-06-12T19:48:00+00:00 | C11_mom_60m | fwd_60m | raw | 8 | 4416876 | -0.00093 | -0.475 | 0.00049 | Momentum-only at 60m horizon raw. Momentum decays slower than 30m noise — does IC strengthen at 60m? |

### OPERATING PROCEDURE — use scripts/run_tool.sh for trainer/tools runs (2026-06-12)

After my #16 stale-image catch (the built trainer was 14h behind the MODEL_FILENAME commit and would
have clobbered the live model), prod committed scripts/run_tool.sh (Manager #11 req): a BLOCKING
freshness gate wrapping every tools-profile run (trainer/backfiller) — it auto-rebuilds a content-stale
image to source before running, refuses if it can't, and tolerates -dirty (built-from-WIP = ahead, not
behind). ADOPT IT: future trainer/backfiller runs go through `scripts/run_tool.sh trainer fwd_30m ...`
(or `make run-tool S=trainer A=fwd_30m`) instead of raw `docker compose run` — then a stale image can't
bite a 5th time. My manual diff-before-run is now the backstop, not the only line.

### EX-DIV CORRECTED BATTERY — DB lock OOM, fixed (2026-06-12)

The corrected-overnight battery's yield query (close from bars_1m) OOM'd twice: bars_1m is a 693-chunk
hypertable, max_locks_per_transaction=64, and a panel-window scan with a time-of-day predicate locks
every chunk in range — multiplied by parallel workers ("out of shared memory / parallel worker"). FIX
(commit 8faabec): SET max_parallel_workers_per_gather=0 + query bars_1m MONTH-BY-MONTH (each statement
touches ~1 month of chunks, under the lock budget). Verified 3409 yields load cleanly. LESSON for future
sandbox queries against bars_1m: never scan it panel-wide in one statement with a non-pruning predicate;
chunk by month and kill parallel workers. (Re-running now; interpretation still held for qa-2's verify.)
| 2026-06-12T19:49:55+00:00 | C11_60m_raw_nocal | fwd_60m | raw | 19 | 4416876 | 0.0196 | 11.554 | 0.00238 | Clean 60m baseline: raw nocalendar (19 feats). Longer horizon, full price+mom set. |

### STRATEGIC FRAMING CORRECTION (Manager, 2026-06-12) — signal inventory + #21 pulled forward

Manager refined my "single-threaded on OFI" framing (it was slightly too pessimistic). HONEST inventory
of our queued/live signal sources after Family B's discard:
- ONE NEW DATA CLASS queued: OFI (v1.2.0, pilot ~6/26).
- ONE in SCOPING: news/event flags (#21) — prod's scoping half PULLED FORWARD to TOMORROW MORNING
  (my pre-empt accepted: with Family B discarded, the queued-signal inventory if OFI fails is empty, so
  price the news option BEFORE we need it; doesn't compete with tonight's batch).
- TWO DERIVED THREADS LIVE (not new data classes, but real): Family A ex-div (may BOTH clean the
  overnight picture AND yield realized features) and #20 sector-neutralization (may extract more from
  existing momentum).
So: not zero-Plan-B. The accurate line is "1 new data class + 1 in scoping + 2 derived threads," not
"single-threaded on OFI." Keep this honest framing in reports.

FORMAL COMMITMENT (Manager): if the ex-div correction CHANGES the overnight survivorship picture, Family
B gets ONE RE-LOOK on the ex-div-corrected labels before its discard is FINAL. (Family B's overnight
survivorship -1.61 was measured on ex-div-CONTAMINATED labels; removing the artifact could in principle
change it. The corrected battery answers this — pending qa-2's verify before interpretation.)
| 2026-06-12T19:50:31+00:00 | C11_overnight_raw_nocal | overnight | raw | 19 | 428024 | 0.0142 | 1.661 | -0.00557 | Clean overnight raw nocalendar. Re-anchor overnight on clean panel (battery showed survivorship; this is the IC-level read). |
| 2026-06-12T19:51:15+00:00 | C11_overnight_mom_rel | overnight | raw | 4 | 428024 | 0.00869 | 1.532 | -0.00106 | Overnight, relative-momentum only. Overnight gap continuation/reversal is where cross-sectional momentum is most plausible. |

## ★ STRATEGY_SHAPES — living backlog beyond cross-sectional L/S ranking (Modeller, 2026-06-12)

Ben supreme standing order: the edge hunt never idles — more strategies, more features, more tickers,
ELEGANT not-too-complex. Manager's catch: EVERYTHING we've tested is ONE shape — cross-sectional L/S
ranking at 30m/overnight. This is the living backlog of OTHER shapes. Each = hypothesis + required
label/data + cheapness (★=trivial/existing data … ★★★★=needs new collection). Grounded in the ACTUAL
data state: news table EMPTY (0 rows, blocks news shapes until #21); CA feed LIVE (corporate_actions_pit);
labels.py builds any fwd_Nm + overnight cheaply; bars_1m queries MUST month-chunk (693-chunk lock limit).

SHAPE 1 — OPEN-GAP DYNAMICS (fade vs follow). ★ CHEAP, data EXISTS.
  Hypothesis: the overnight gap (open vs prior close) either continues (momentum) or reverts (fade),
  conditionally. We already have gap_from_open as a feature but have NEVER used it as the STRATEGY AXIS
  with a gap-anchored label. Label: open-to-close return (fwd from the 09:30 open to 16:00 close) — NEW
  but cheap (forward_return_series machinery, anchored at the open bar). Condition the fade/follow on
  gap size, overnight volume, prior-day range. Elegant, classic, testable on existing bars.

SHAPE 2 — FIRST-30-MIN RANGE BREAKOUT. ★ CHEAP, data EXISTS.
  Hypothesis: names that break their 09:30-10:00 high/low continue in the breakout direction intraday
  (opening-range breakout, a well-known intraday shape). Label: 10:00->close (or 10:00->fwd_120m) return.
  Features: position vs the first-30-min range, first-30-min volume vs ADV. NEW label (fwd from 10:00),
  cheap. Single-name TIME-SERIES signal, not cross-sectional — a genuinely different shape.

SHAPE 3 — POST-CORPORATE-ACTION DRIFT/REVERSAL. ★★ CHEAP-ish, CA data LIVE NOW.
  Hypothesis: names post-ex-dividend (or post-split) exhibit drift or reversal in the following days
  (dividend-capture unwind, post-split retail flow). Label: fwd 1-5 day return anchored on ex_date.
  Data: corporate_actions_pit (LIVE). Event-anchored window label = NEW. Distinct from the ex-div LABEL
  HYGIENE work — this TRADES the post-event drift rather than cleaning it out. Cheap, uses live data.

SHAPE 4 — VOLUME / TRADE-INTENSITY SHOCK REACTION. ★★ data exists (bars) but needs OFI for the good version.
  Hypothesis: a volume/range shock (today's volume >> trailing avg) predicts next-day reversal or
  continuation. Label: overnight or fwd_120m. Features: volume_z, range_z (cheap from bars). The richer
  version wants trade-intensity/OFI (M2-gated). The bar-only version is CHEAP and testable now.

SHAPE 5 — SECTOR-RELATIVE MEAN REVERSION. ★★ needs sector_map (#20, landing).
  Hypothesis: a name that has diverged from its SECTOR (not the whole universe) mean-reverts. This is
  cross-sectional but SECTOR-NEUTRALIZED — a different axis than universe-relative. Label: existing
  fwd_30m/overnight but demeaned WITHIN sector. Data: sector_map (#20, post-batch). Cheap once sector lands.

SHAPE 6 — EVENT-REACTION (post-news drift/reversal). ★★★★ BLOCKED — news table EMPTY.
  Hypothesis: post-headline drift or overreaction-reversal. Label: event-anchored fwd window. Data: needs
  the news table populated (#21 scoping tomorrow). Logged as the highest-potential-but-blocked shape;
  revisit when news lands.

SHAPE 7 — HORIZON ENSEMBLE (30m signal GATES overnight holds). ★★ cheap, composition of existing.
  Hypothesis: use the 30m cross-sectional signal not to TRADE intraday (uneconomic) but to GATE which
  names to hold overnight — i.e. the 30m rank as a FILTER on the overnight book. Elegant: combines two
  things we have without new data. Label: overnight, conditioned on the 30m prediction. Cheap.

### THIS WEEKEND — picked + spec'd (the 2-3 most promising, into the queue):
1. SHAPE 1 (open-gap dynamics) — needs the open-to-close label. SPEC: fwd label anchored at the 09:30
   RTH open bar to the 16:00 close, cross-sectionally demeaned (reuse cross_sectional_excess). Cheapest
   high-value new shape.
2. SHAPE 2 (opening-range breakout) — needs the 10:00->close label + first-30-min-range features.
3. SHAPE 7 (horizon ensemble) — no new label; compose existing 30m + overnight. Pure harness work.
These 3 need NEW LABELS (open-to-close, 10:00-anchored) — spec'd below for the label builder.
| 2026-06-12T19:51:58+00:00 | C11_overnight_rank_nocal | overnight | rank | 19 | 428024 | 0.01891 | 2.121 | 0.00012 | Overnight rank label nocalendar. Trading-aligned overnight ranking on clean data. |
| 2026-06-12T19:52:41+00:00 | C11_overnight_lambdarank_nocal | overnight | lambdarank | 19 | 428024 | 0.03583 | 2.766 | 0.00202 | Overnight lambdarank nocalendar. The config that looked best (pre-survivorship) — re-check IC/canary on clean panel. |

## ★ EX-DIV CORRECTED OVERNIGHT BATTERY — VERDICT (Modeller, 2026-06-12, qa-2-verified, hold lifted)

qa-2 verified the ex-div diagnostic on all 4 angles (bucket reproduction, PIT alignment, no double-counts,
magnitude) — interpretation hold LIFTED. Ran the corrected overnight battery (experiments/
exdiv_corrected_battery.py): RAW v1.1.1 overnight labels vs EX-DIV-CORRECTED (dividend yield added back
to the 3,291 affected nights, 0.769% of labels, IN-MEMORY — frozen labels NEVER written). 4 labels × 2 bases:

  label       | RAW: IC   canary  bkeven SURV  || FIX: IC   canary  bkeven SURV
  raw         | +0.01420 -0.0056  3.2bps -1.79 || +0.00956 -0.0061  2.32bps -2.18
  rank        | +0.01891 +0.0001  2.91bps -1.20 || +0.01657 -0.0023  2.88bps -1.39
  vol_scaled  | +0.00761 -0.0046  0.97bps -1.68 || +0.00656 -0.0065  1.72bps -1.70
  lambdarank  | +0.03583 +0.0020  9.65bps -0.35 || +0.03386 +0.0091  9.64bps -0.15

VERDICT: removing the ex-div artifact LOWERS the apparent overnight IC on EVERY config (e.g. raw
0.0142->0.0096, lambdarank 0.0358->0.0339) — CONFIRMING part of the raw overnight "signal" was the
model predicting the mechanical, predictable ex-div drop, NOT alpha. BUT the survivorship-neutralized
sharpe stays NEGATIVE everywhere (raw -1.79->-2.18, lambdarank -0.35->-0.15) — if anything slightly more
negative. So the overnight signal was SURVIVORSHIP before correction and remains survivorship after. The
ex-div correction is a genuine LABEL-HYGIENE improvement (removes a known contaminant, deflates spuriously-
inflated IC) but reveals NO hidden overnight alpha. No tradeable overnight edge, corrected or not.

FAMILY B RE-LOOK RESOLVED: the formal commitment was "re-look Family B on corrected labels IF the
correction CHANGES the overnight survivorship picture." It did NOT — survivorship stays negative
everywhere. So Family B's DISCARD is FINAL; no re-look warranted (its overnight survivorship -1.61 was not
an ex-div artifact).

PRODUCTION-FIX NUANCE (qa-2, for the Tier-1 quantlib/labels.py PR): the correction slightly OVER-corrects
(+4.8bps net vs baseline — the yield denominator is marginally off; I use the 15:59 prior close, should
likely be the official daily close / adjusted basis). Refine the denominator in the production fix. The
DIRECTION + verdict are unaffected; only the last ~5bps of precision. The ex-div label hygiene is still
worth shipping (Tier-1 PR, qa-2 to review) because it removes a known mechanical contaminant from every
overnight experiment — but it is a CORRECTNESS fix, not an edge.
| 2026-06-12T19:54:38+00:00 | C11_loo_mom_1d | fwd_30m | raw | 7 | 4840765 | -0.0016 | -1.077 | -0.00052 | Leave-one-out: momentum minus mom_1d. Marginal contribution of mom_1d — does dropping it move IC? |
| 2026-06-12T19:56:30+00:00 | C11_loo_mom_3d | fwd_30m | raw | 7 | 4840765 | -0.00113 | -0.741 | 0.00106 | Leave-one-out: momentum minus mom_3d. Marginal contribution of mom_3d — does dropping it move IC? |
| 2026-06-12T19:58:28+00:00 | C11_loo_mom_5d | fwd_30m | raw | 7 | 4840765 | -0.00129 | -0.86 | -0.00012 | Leave-one-out: momentum minus mom_5d. Marginal contribution of mom_5d — does dropping it move IC? |

## TASK #22 — composable label/feature materialization: MODELLER REQUIREMENTS (2026-06-12)

I'm the requirements partner; prod-architect-2 (Architect hat) designs. These are what I, the research
CONSUMER, need from a composable label/feature layer — grounded in the THREE workarounds I needed in ONE
session today (in-memory ex-div label correction; sandbox Family B derived features; in-experiment new
open-to-close/ten-to-close labels), each because the panel is a monolithic rebuild.

THE PROBLEM (concrete): adding a new LABEL (open-to-close, event-anchored, fwd_120m) or a new FEATURE
GROUP (dispersion, sector-demeaned, OFI) currently requires either (a) a full panel rebuild — hours,
prod-owned, serialized, blocks everyone — or (b) an in-experiment hack that doesn't persist, can't be
shared, and re-computes every run. Neither scales to Ben's "more strategies, more labels, more tickers"
standing order. "Iterate on any strategy cheaply" is FALSE at the label layer today.

REQUIREMENTS (what would make me 10x faster):
1. LABEL VERSIONING (the first brick, already flagged): labels need a (basis/version) column so multiple
   label definitions coexist — canonical fwd_30m AND ex-div-corrected fwd_30m AND open_to_close — without
   overwriting (the trap that destroyed v1.1.0's labels). A label is keyed (symbol, ts, horizon, version).
2. INCREMENTAL MATERIALIZATION: I define a new label/feature as a pure function over existing stored
   inputs (bars, existing features, CA/news/sector tables) and materialize it for the EXISTING panel
   WITHOUT recomputing the 5.5M-row feature panel. A new label should cost minutes (its own compute), not
   a full rebuild.
3. COMPOSABILITY: features and labels are independent layers joined at experiment time. Adding feature
   group X must not touch labels; adding label Y must not touch features. (Today they're entangled in one
   feature_vectors+labels rebuild.)
4. PROVENANCE: each materialized label/feature records its definition + input versions + computed_at, so a
   verdict maps to an exact, reproducible (feature_version × label_version) pair — preserving the M1-style
   pinned-artifact discipline that let me freeze v1.1.1 for the verdict.
5. SELF-SERVE FOR THE MODELLER: I can define + materialize a Tier-2 EXPLORATORY label/feature in the
   sandbox (my lane, fast), and PROMOTE it to a Tier-1 production materialization via PR when it proves
   out — same flow as code. The exploratory→production path must be a promotion, not a rewrite.
NICE-TO-HAVE: a registry/catalog of available labels+features so I (and the experimenter queue) can
reference them by name. CONSTRAINT to preserve: the experimenter's load_panel reads feature_sets.names +
joins labels — whatever the design, keep a clean "give me (features, labels) for version pair P" loader.

PRIORITY ORDER for the design memo: label-versioning column FIRST (unblocks the ex-div hygiene PR + every
new-label shape), then incremental label materialization, then the feature side. The ex-div correctness
PR is the immediate forcing function — it NEEDS label-versioning to persist the corrected labels without
overwriting the frozen canonical ones.
