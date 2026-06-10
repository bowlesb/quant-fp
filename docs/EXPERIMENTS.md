# Experiment Log

Append-only history of all experiments (the Modeller's exploration). IC is vs the actual forward return; the shuffle canary is the leakage arbiter. Thin panel -> exploration, not edge.

| run_at | id | horizon | label | feats | rows | mean_IC | NW_t | canary | hypothesis |
|---|---|---|---|---|---|---|---|---|---|
| 2026-06-10T21:57:30+00:00 | E0_raw_18 | fwd_30m | raw | 18 | 570481 | 0.02052 | 2.976 | -0.00107 | Baseline: regression on raw fwd_30m excess, all 18 features. Reproduces the trainer result; reference point. |
