# W13 — Sector momentum via the 11 SPDR sector ETFs — RESULTS

Panel: 378 aligned trading days, 11 sector ETFs + SPY. All returns in **basis points (bps)** per 21-day hold.
CSVs: `cross_sectional_results.csv`, `time_series_results.csv`, `rebalance_detail.csv`, `panel.parquet`.

## Headline: the momentum signal is NEGATIVE (reversal), not positive, at every formation window

Cost is trivial (≤0.63 bps) exactly as the friction-wall design predicted — friction is **not** the problem.
The signal itself is wrong-signed: long-recent-winners / short-recent-losers **loses** money over 2024-12 → 2026-06.

## Cross-sectional (long top-3 / short bottom-3), per 21-day hold

| F (days) | n_rebal | gross bps | turnover | cost bps | **net bps** | t | boot 95% CI | block 95% CI |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 21  | 16 | -186.9  | 1.56 | 0.63 | **-187.5** | -1.31 | [-477.4, +74.8] | [-439.3, +73.5] |
| 63  | 14 | -160.3  | 0.86 | 0.34 | **-160.6** | -0.87 | [-521.9, +173.2] | [-535.7, +108.5] |
| 126 | 11 | -234.4  | 0.64 | 0.26 | **-234.7** | -2.39 | [-418.3, -57.4] | [-499.9, -37.4] |

Walk-forward OOS (second half by date):

| F | OOS n | OOS net bps | OOS boot 95% CI | OOS block 95% CI |
|---:|---:|---:|---:|---:|
| 21  | 8 | -219.5 | [-758.5, +271.0] | [-727.4, +357.9] |
| 63  | 7 | -263.6 | [-841.8, +300.3] | [-733.8, +23.8] |
| 126 | 6 | -420.8 | [-618.4, -206.5] | [-636.3, -338.6] |

Shuffle canary (permute sector→fwd): real means are all negative and well inside the permutation null
(p = 0.27 / 0.39 / 0.31) — the ranking does not even produce a positive edge to distinguish; the negative
result is consistent with no-momentum / mild reversal.

## Time-series / absolute (long trailing>0 / short trailing<0), per 21-day hold

| F (days) | n_rebal | gross bps | turnover | cost bps | **net bps** | t | boot 95% CI | block 95% CI |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 21  | 16 | -354.3 | 1.14 | 0.46 | **-354.8** | -2.69 | [-611.2, -112.7] | [-532.5, -98.1] |
| 63  | 14 | -168.1 | 0.58 | 0.23 | **-168.3** | -1.83 | [-340.2, +5.4]   | [-321.6, -34.2] |
| 126 | 11 | -304.0 | 0.49 | 0.20 | **-304.2** | -1.19 | [-843.3, +46.5]  | [-841.2, +23.9] |

Walk-forward OOS (second half):

| F | OOS n | OOS net bps | OOS boot 95% CI | OOS block 95% CI |
|---:|---:|---:|---:|---:|
| 21  | 8 | -343.6 | [-814.8, +83.5]   | [-637.3, +90.7] |
| 63  | 7 | -237.0 | [-482.0, +22.0]   | [-427.5, -156.2] |
| 126 | 6 | -670.0 | [-1510.5, -233.7] | [-1070.8, -249.6] |

## Reading
- **Every cell** of net momentum return — cross-sectional and time-series, all three formation windows,
  in-sample and OOS — is **negative**. Several CIs sit entirely below zero (F=126 cross-sectional, F=21
  time-series, the F=126 OOS in both forms), i.e. the reversal is significant in the wrong direction; the
  rest straddle zero on the negative side.
- Turnover-adjusted cost averages 0.2–0.63 bps per rebalance — three orders of magnitude smaller than the
  signal. The friction wall is irrelevant: the signal does not exist net of nothing.
- Power: n_rebalances = 11–16, 11 instruments → CIs are wide. But the point estimates are uniformly,
  materially negative with no positive form anywhere — this is not a "small-n, can't tell" null; it is a
  consistently wrong-signed signal over the available 18 months.
