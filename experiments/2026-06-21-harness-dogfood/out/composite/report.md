# Strategy Harness report

model=**composite**  cadence=daily  label_horizon=1d/30m  L/S frac=10%  capital=$1,000,000  universe_top=500  folds=5
panel=57,265 rows x 13 features x 496 symbols  test_timestamps=97
panel_load=0.147s  fit+apply=1.228s  **total=1.375s**

## a. MONEY (the configured basket, net of per-name cost)

- **total P&L**: $215,575 on $1,000,000 book
- **net return**: 21.56% over the test span
- **after-cost Sharpe**: 1.13 (annualized)
- **max drawdown**: 15.77%
- **mean turnover/period**: 1.909
- **breakeven cost**: 13.57 bps one-way (charged 3.37 bps median)
- **periods**: 96

## b. PERCENTILE-THRESHOLD CURVE (the headline — conservative-application analysis)

As the cut shrinks (more selective), does directional precision and $/trade improve?

| cut (top/bot) | n_trades | precision | mean_fwd_ret | $/trade | total $ P&L | net/period | Sharpe_net |
|---|---|---|---|---|---|---|---|
| 1% | 766 | 0.5183 | 36.49bps | $814 | $623,266 | 64.924bps | 1.70 |
| 2% | 1,724 | 0.5122 | 25.41bps | $242 | $416,996 | 43.437bps | 1.61 |
| 5% | 4,590 | 0.5159 | 16.77bps | $57 | $259,716 | 27.054bps | 1.31 |
| 10% | 9,344 | 0.5121 | 12.62bps | $20 | $186,175 | 19.393bps | 1.13 |
| 20% | 18,718 | 0.5079 | 8.43bps | $6 | $113,425 | 11.815bps | 0.91 |
| 33% | 31,056 | 0.5028 | 4.57bps | $2 | $49,614 | 5.168bps | 0.52 |
| 50% | 47,084 | 0.5003 | 2.26bps | $0 | $14,579 | 1.519bps | 0.21 |

context model diagnostics: sign-AUC=0.5042  rank-IC=0.0008

## c. BASELINES (the trust gate — the curve must beat these)

- **predict-zero** (no signal): total P&L = $0
- **shuffle** (within-timestamp label permutation — the leakage/overfit null):

| cut | precision | mean_fwd_ret | total $ P&L | Sharpe_net |
|---|---|---|---|---|
| 1% | 0.4869 | -0.68bps | -$101,585 | -0.83 |
| 2% | 0.5000 | 7.85bps | $69,699 | 0.79 |
| 5% | 0.5070 | 5.56bps | $40,575 | 0.68 |
| 10% | 0.5041 | 3.01bps | -$2,694 | -0.05 |
| 20% | 0.5032 | 1.37bps | -$22,471 | -0.70 |
| 33% | 0.4996 | -0.76bps | -$53,564 | -2.16 |
| 50% | 0.5002 | 0.17bps | -$26,637 | -1.24 |

shuffle sign-AUC=0.5007 (~0.5 expected); shuffle rank-IC=0.0022 (~0 expected).

> A PASS-grade result needs precision and $/trade to RISE as the cut shrinks AND the real curve to dominate the shuffle/predict-zero baselines at every cut. Read this as an idealized upper bound (frictionless basket, survivorship caveat), not a live realized P&L.
