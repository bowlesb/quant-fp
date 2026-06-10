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
