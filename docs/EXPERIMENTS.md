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
