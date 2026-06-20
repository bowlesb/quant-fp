# Strategy Harness report

model=**composite**  cadence=daily  label_horizon=1d/30m  L/S frac=10%  capital=$1,000,000  universe_top=500  folds=5
panel=56,814 rows x 13 features x 495 symbols  test_timestamps=97
panel_load=0.091s  fit+apply=0.859s  **total=0.95s**

## a. MONEY (the configured basket, net of per-name cost)

- **total P&L**: $204,515 on $1,000,000 book
- **net return**: 20.45% over the test span
- **after-cost Sharpe**: 1.09 (annualized)
- **max drawdown**: 15.80%
- **mean turnover/period**: 1.909
- **breakeven cost**: 13.09 bps one-way (charged 3.37 bps median)
- **periods**: 95

## b. PERCENTILE-THRESHOLD CURVE (the headline — conservative-application analysis)

As the cut shrinks (more selective), does directional precision and $/trade improve?

| cut (top/bot) | n_trades | precision | mean_fwd_ret | $/trade | total $ P&L | net/period | Sharpe_net |
|---|---|---|---|---|---|---|---|
| 1% | 760 | 0.5184 | 36.05bps | $788 | $598,587 | 63.009bps | 1.63 |
| 2% | 1,710 | 0.5117 | 25.02bps | $231 | $395,789 | 41.662bps | 1.54 |
| 5% | 4,552 | 0.5160 | 16.89bps | $55 | $250,926 | 26.413bps | 1.29 |
| 10% | 9,260 | 0.5131 | 12.63bps | $19 | $175,442 | 18.468bps | 1.09 |
| 20% | 18,558 | 0.5091 | 8.82bps | $6 | $115,463 | 12.154bps | 0.93 |
| 33% | 30,682 | 0.5032 | 4.47bps | $1 | $43,834 | 4.614bps | 0.47 |
| 50% | 46,584 | 0.5009 | 2.06bps | $0 | $8,360 | 0.880bps | 0.12 |

context model diagnostics: sign-AUC=0.5050  rank-IC=-0.0009

## c. BASELINES (the trust gate — the curve must beat these)

- **predict-zero** (no signal): total P&L = $0
- **shuffle** (within-timestamp label permutation — the leakage/overfit null):

| cut | precision | mean_fwd_ret | total $ P&L | Sharpe_net |
|---|---|---|---|---|
| 1% | 0.5000 | -6.60bps | -$211,637 | -1.96 |
| 2% | 0.4936 | -9.60bps | -$261,894 | -2.60 |
| 5% | 0.4910 | -7.30bps | -$208,835 | -3.58 |
| 10% | 0.4944 | -5.98bps | -$175,995 | -4.02 |
| 20% | 0.4973 | -4.03bps | -$126,725 | -4.10 |
| 33% | 0.4994 | -2.67bps | -$90,577 | -3.92 |
| 50% | 0.4984 | -2.20bps | -$71,859 | -3.56 |

shuffle sign-AUC=0.4980 (~0.5 expected); shuffle rank-IC=-0.0079 (~0 expected).

> A PASS-grade result needs precision and $/trade to RISE as the cut shrinks AND the real curve to dominate the shuffle/predict-zero baselines at every cut. Read this as an idealized upper bound (frictionless basket, survivorship caveat), not a live realized P&L.
