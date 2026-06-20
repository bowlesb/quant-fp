# Strategy Harness report

model=**ridge**  cadence=daily  label_horizon=1d/30m  L/S frac=10%  capital=$1,000,000  universe_top=500  folds=5
panel=56,814 rows x 13 features x 495 symbols  test_timestamps=97
panel_load=0.086s  fit+apply=1.185s  **total=1.271s**

## a. MONEY (the configured basket, net of per-name cost)

- **total P&L**: $254,344 on $1,000,000 book
- **net return**: 25.43% over the test span
- **after-cost Sharpe**: 1.43 (annualized)
- **max drawdown**: 11.76%
- **mean turnover/period**: 2.338
- **breakeven cost**: 13.28 bps one-way (charged 3.37 bps median)
- **periods**: 95

## b. PERCENTILE-THRESHOLD CURVE (the headline — conservative-application analysis)

As the cut shrinks (more selective), does directional precision and $/trade improve?

| cut (top/bot) | n_trades | precision | mean_fwd_ret | $/trade | total $ P&L | net/period | Sharpe_net |
|---|---|---|---|---|---|---|---|
| 1% | 760 | 0.4987 | 27.77bps | $582 | $442,209 | 46.548bps | 1.34 |
| 2% | 1,710 | 0.5146 | 28.42bps | $268 | $457,574 | 48.166bps | 2.00 |
| 5% | 4,552 | 0.5037 | 19.04bps | $62 | $284,016 | 29.896bps | 1.59 |
| 10% | 9,260 | 0.5083 | 15.45bps | $24 | $222,396 | 23.410bps | 1.43 |
| 20% | 18,558 | 0.5066 | 8.81bps | $6 | $104,762 | 11.028bps | 0.82 |
| 33% | 30,682 | 0.5075 | 6.39bps | $2 | $69,296 | 7.294bps | 0.70 |
| 50% | 46,584 | 0.5056 | 4.48bps | $1 | $46,188 | 4.862bps | 0.64 |

context model diagnostics: sign-AUC=0.5072  rank-IC=0.0054

## c. BASELINES (the trust gate — the curve must beat these)

- **predict-zero** (no signal): total P&L = $0
- **shuffle** (within-timestamp label permutation — the leakage/overfit null):

| cut | precision | mean_fwd_ret | total $ P&L | Sharpe_net |
|---|---|---|---|---|
| 1% | 0.4632 | -9.25bps | -$261,277 | -1.58 |
| 2% | 0.4906 | 9.61bps | $100,056 | 1.00 |
| 5% | 0.4914 | -0.12bps | -$79,745 | -1.19 |
| 10% | 0.4918 | -4.61bps | -$159,915 | -3.45 |
| 20% | 0.5001 | 0.04bps | -$61,840 | -1.87 |
| 33% | 0.5002 | 0.47bps | -$42,878 | -1.76 |
| 50% | 0.5003 | 0.75bps | -$24,532 | -1.33 |

shuffle sign-AUC=0.4996 (~0.5 expected); shuffle rank-IC=0.0011 (~0 expected).

> A PASS-grade result needs precision and $/trade to RISE as the cut shrinks AND the real curve to dominate the shuffle/predict-zero baselines at every cut. Read this as an idealized upper bound (frictionless basket, survivorship caveat), not a live realized P&L.
