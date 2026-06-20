# Strategy Harness report

model=**gbm**  cadence=daily  label_horizon=1d/30m  L/S frac=10%  capital=$1,000,000  universe_top=500  folds=5
panel=56,814 rows x 13 features x 495 symbols  test_timestamps=97
panel_load=0.113s  fit+apply=5.807s  **total=5.92s**

## a. MONEY (the configured basket, net of per-name cost)

- **total P&L**: $18,992 on $1,000,000 book
- **net return**: 1.90% over the test span
- **after-cost Sharpe**: -0.12 (annualized)
- **max drawdown**: 19.70%
- **mean turnover/period**: 2.576
- **breakeven cost**: 2.83 bps one-way (charged 3.37 bps median)
- **periods**: 95

## b. PERCENTILE-THRESHOLD CURVE (the headline — conservative-application analysis)

As the cut shrinks (more selective), does directional precision and $/trade improve?

| cut (top/bot) | n_trades | precision | mean_fwd_ret | $/trade | total $ P&L | net/period | Sharpe_net |
|---|---|---|---|---|---|---|---|
| 1% | 760 | 0.5197 | 27.91bps | $563 | $427,930 | 45.045bps | 1.43 |
| 2% | 1,710 | 0.5216 | 21.93bps | $188 | $321,462 | 33.838bps | 1.71 |
| 5% | 4,552 | 0.5070 | 11.65bps | $30 | $135,195 | 14.231bps | 1.08 |
| 10% | 9,260 | 0.5029 | 3.48bps | -$1 | -$11,500 | -1.210bps | -0.12 |
| 20% | 18,558 | 0.5066 | 6.22bps | $2 | $43,577 | 4.587bps | 0.67 |
| 33% | 30,682 | 0.5096 | 7.63bps | $3 | $80,242 | 8.447bps | 1.62 |
| 50% | 46,584 | 0.5105 | 6.61bps | $2 | $76,590 | 8.062bps | 2.03 |

context model diagnostics: sign-AUC=0.5090  rank-IC=0.0147

## c. BASELINES (the trust gate — the curve must beat these)

- **predict-zero** (no signal): total P&L = $0
- **shuffle** (within-timestamp label permutation — the leakage/overfit null):

| cut | precision | mean_fwd_ret | total $ P&L | Sharpe_net |
|---|---|---|---|---|
| 1% | 0.4803 | 7.70bps | $43,867 | 0.23 |
| 2% | 0.4772 | -5.77bps | -$204,972 | -1.53 |
| 5% | 0.4991 | 3.36bps | -$22,071 | -0.30 |
| 10% | 0.4988 | 1.44bps | -$53,284 | -1.10 |
| 20% | 0.5017 | 1.98bps | -$37,278 | -1.11 |
| 33% | 0.5039 | 1.42bps | -$38,168 | -1.55 |
| 50% | 0.5045 | 2.62bps | $671 | 0.03 |

shuffle sign-AUC=0.5031 (~0.5 expected); shuffle rank-IC=0.0030 (~0 expected).

> A PASS-grade result needs precision and $/trade to RISE as the cut shrinks AND the real curve to dominate the shuffle/predict-zero baselines at every cut. Read this as an idealized upper bound (frictionless basket, survivorship caveat), not a live realized P&L.
