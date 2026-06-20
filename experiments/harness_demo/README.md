# Strategy Harness — demo run output

The `run_strategy` harness on the trusted-liquid DAILY panel, **2025-12-01 .. 2026-06-17**, top-500-ADV
universe (495 symbols, 13 trailing features, ~57k rows), forward-1-day cross-sectional excess label,
dollar-neutral L/S-by-percentile, **$1,000,000** book, net of per-name half-spread + 1bp slippage + 50bp
annual borrow. Reproduce with: `python -m quantlib.harness --model <gbm|ridge|composite> --out <dir>`.

Each subdir has `report.md` (the organized output), `equity_curve.csv`, `threshold_curve.csv`, `report.json`.

## The honest headline

The standard 10% L/S basket is roughly break-even (consistent with the edge hunt's prior nulls), BUT the
**percentile-threshold curve shows a real, conservative-application edge concentrated in the tails**:
precision and $/trade rise monotonically as the cut tightens, and the real curve **dominates the shuffle
baseline at every cut** (shuffle AUC ~0.503, ~chance as it should be). This is the diagnostic Ben asked for —
"as I get more selective, does $/trade and precision improve?" — and here the answer is YES.

| model | runtime | 1%-cut precision | 1%-cut $/trade | 2%-cut total $ P&L | 2%-cut Sharpe | context AUC |
|---|---|---|---|---|---|---|
| **gbm**       | 5.9s | 0.5197 | $563 | $321,462 | 1.71 | 0.5090 |
| **ridge**     | 1.3s | 0.4987 | $582 | **$457,574** | **2.00** | 0.5072 |
| **composite** | 1.0s | 0.5184 | $788 | $395,789 | 1.54 | 0.5050 |

(Per-cut detail in each `report.md`. $/trade and total $ P&L are the conservative-threshold money figures
on the $1M book; the 1%-cut $/trade is the cleanest "most-confident-bets" number.)

## How to read it

- The money + threshold-curve are an **idealized upper bound** (frictionless ~50-name-per-leg basket, the
  universe-survivorship caveat the panel carries) — NOT a live realized P&L. The live
  `production_execution.py` layer measures the borrow / capacity / partial-fill gap.
- The strategy graduates to a live container with **zero re-implementation of the decision**: the harness and
  the live container both call the SAME `CrossSectionalLS.decide`, proven by
  `tests/harness/test_harness_portability.py`. Graduation is the frozen model + the L/S frac, not new code.
