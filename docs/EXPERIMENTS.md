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
