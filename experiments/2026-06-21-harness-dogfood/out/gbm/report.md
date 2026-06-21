# Strategy Harness report

model=**gbm**  cadence=daily  label_horizon=1d/30m  L/S frac=10%  capital=$1,000,000  universe_top=500  folds=5
panel=57,265 rows x 13 features x 496 symbols  test_timestamps=97
panel_load=0.113s  fit+apply=8.18s  **total=8.294s**

## a. MONEY (the configured basket, net of per-name cost)

- **total P&L**: $33,820 on $1,000,000 book
- **net return**: 3.38% over the test span
- **after-cost Sharpe**: 0.03 (annualized)
- **max drawdown**: 20.30%
- **mean turnover/period**: 2.579
- **breakeven cost**: 3.41 bps one-way (charged 3.37 bps median)
- **periods**: 96

## b. PERCENTILE-THRESHOLD CURVE (the headline — conservative-application analysis)

As the cut shrinks (more selective), does directional precision and $/trade improve?

| cut (top/bot) | n_trades | precision | mean_fwd_ret | $/trade | total $ P&L | net/period | Sharpe_net |
|---|---|---|---|---|---|---|---|
| 1% | 766 | 0.5117 | 25.82bps | $517 | $395,843 | 41.234bps | 1.25 |
| 2% | 1,724 | 0.5058 | 20.23bps | $169 | $290,861 | 30.298bps | 1.53 |
| 5% | 4,590 | 0.5074 | 11.33bps | $29 | $132,397 | 13.791bps | 0.98 |
| 10% | 9,344 | 0.5031 | 4.21bps | $0 | $2,599 | 0.271bps | 0.03 |
| 20% | 18,718 | 0.5065 | 7.74bps | $4 | $73,831 | 7.691bps | 1.08 |
| 33% | 31,056 | 0.5077 | 7.85bps | $3 | $84,600 | 8.813bps | 1.64 |
| 50% | 47,084 | 0.5057 | 5.78bps | $1 | $61,063 | 6.361bps | 1.58 |

context model diagnostics: sign-AUC=0.5068  rank-IC=0.0133

## c. BASELINES (the trust gate — the curve must beat these)

- **predict-zero** (no signal): total P&L = $0
- **shuffle** (within-timestamp label permutation — the leakage/overfit null):

| cut | precision | mean_fwd_ret | total $ P&L | Sharpe_net |
|---|---|---|---|---|
| 1% | 0.5117 | -14.32bps | -$382,634 | -2.46 |
| 2% | 0.5104 | 0.69bps | -$82,252 | -0.79 |
| 5% | 0.5094 | 3.30bps | -$21,849 | -0.39 |
| 10% | 0.5007 | -2.51bps | -$126,696 | -3.02 |
| 20% | 0.5007 | -1.19bps | -$97,705 | -3.37 |
| 33% | 0.4982 | -1.91bps | -$102,249 | -5.09 |
| 50% | 0.5008 | 0.19bps | -$46,112 | -2.85 |

shuffle sign-AUC=0.5000 (~0.5 expected); shuffle rank-IC=0.0010 (~0 expected).

> A PASS-grade result needs precision and $/trade to RISE as the cut shrinks AND the real curve to dominate the shuffle/predict-zero baselines at every cut. Read this as an idealized upper bound (frictionless basket, survivorship caveat), not a live realized P&L.
