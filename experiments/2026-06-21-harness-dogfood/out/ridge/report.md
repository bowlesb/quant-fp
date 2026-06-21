# Strategy Harness report

model=**ridge**  cadence=daily  label_horizon=1d/30m  L/S frac=10%  capital=$1,000,000  universe_top=500  folds=5
panel=57,265 rows x 13 features x 496 symbols  test_timestamps=97
panel_load=0.116s  fit+apply=0.99s  **total=1.106s**

## a. MONEY (the configured basket, net of per-name cost)

- **total P&L**: $242,546 on $1,000,000 book
- **net return**: 24.25% over the test span
- **after-cost Sharpe**: 1.34 (annualized)
- **max drawdown**: 11.90%
- **mean turnover/period**: 2.348
- **breakeven cost**: 12.59 bps one-way (charged 3.37 bps median)
- **periods**: 96

## b. PERCENTILE-THRESHOLD CURVE (the headline — conservative-application analysis)

As the cut shrinks (more selective), does directional precision and $/trade improve?

| cut (top/bot) | n_trades | precision | mean_fwd_ret | $/trade | total $ P&L | net/period | Sharpe_net |
|---|---|---|---|---|---|---|---|
| 1% | 766 | 0.5065 | 32.74bps | $714 | $546,995 | 56.979bps | 1.66 |
| 2% | 1,724 | 0.5104 | 25.30bps | $235 | $405,082 | 42.196bps | 1.76 |
| 5% | 4,590 | 0.5017 | 18.96bps | $64 | $292,485 | 30.467bps | 1.61 |
| 10% | 9,344 | 0.5065 | 14.64bps | $22 | $210,104 | 21.886bps | 1.34 |
| 20% | 18,718 | 0.5052 | 8.08bps | $5 | $91,778 | 9.560bps | 0.70 |
| 33% | 31,056 | 0.5066 | 6.21bps | $2 | $66,934 | 6.972bps | 0.66 |
| 50% | 47,084 | 0.5050 | 4.26bps | $1 | $42,697 | 4.448bps | 0.59 |

context model diagnostics: sign-AUC=0.5063  rank-IC=0.0050

## c. BASELINES (the trust gate — the curve must beat these)

- **predict-zero** (no signal): total P&L = $0
- **shuffle** (within-timestamp label permutation — the leakage/overfit null):

| cut | precision | mean_fwd_ret | total $ P&L | Sharpe_net |
|---|---|---|---|---|
| 1% | 0.5065 | -0.52bps | -$102,799 | -0.84 |
| 2% | 0.5029 | 4.91bps | $11,405 | 0.14 |
| 5% | 0.5085 | 2.30bps | -$34,245 | -0.66 |
| 10% | 0.5055 | 0.61bps | -$61,023 | -1.57 |
| 20% | 0.5012 | -2.46bps | -$109,738 | -3.45 |
| 33% | 0.5022 | -1.83bps | -$87,922 | -3.77 |
| 50% | 0.5026 | 0.32bps | -$33,535 | -1.74 |

shuffle sign-AUC=0.5026 (~0.5 expected); shuffle rank-IC=0.0011 (~0 expected).

> A PASS-grade result needs precision and $/trade to RISE as the cut shrinks AND the real curve to dominate the shuffle/predict-zero baselines at every cut. Read this as an idealized upper bound (frictionless basket, survivorship caveat), not a live realized P&L.
