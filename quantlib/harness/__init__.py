"""The train -> apply -> evaluate STRATEGY HARNESS (Ben's top deliverable).

`run_strategy(config) -> StrategyReport` turns "I have a panel of (tickers x features x time)" into
"here is the money this long/short-by-percentile strategy would have made, the percentile-threshold
diagnostic curve, and the shuffle / predict-zero baselines" — in ONE configurable, fast call.

The make-or-break invariant (docs/STRATEGY_HARNESS.md, building on STRATEGY_BATTERY_PORTABILITY.md):
the model-application logic is the SHARED `quantlib.strategy_core.CrossSectionalLS.decide` — written
ONCE, applied (a) VECTORIZED over the whole panel here (the fast batch backtest) AND (b) per-single-
vector in a live container's `decide()` over a bus `FeatureView`. TRAINING is offline (walk-forward on
the train fold); the trained model is a frozen `RankModel` artifact the SAME `decide` loads. A harness-
validated strategy drops into a live container with ZERO re-implementation of the decision — proven by
`tests/harness/test_harness_portability.py` (the SAME decide row-by-row == the vectorized panel apply).

Entry points:

    from quantlib.harness import HarnessConfig, run_strategy
    report = run_strategy(HarnessConfig(daily_cache="experiments/data/battery_daily_cache.parquet"))
    print(report.summary_md)          # money + the percentile-threshold curve + baselines
    report.equity_curve               # ($-on-capital) per period

or the CLI:  python -m quantlib.harness --daily-cache <path> --capital 1000000 --out /tmp/harness
"""
from __future__ import annotations

from quantlib.harness.config import HarnessConfig
from quantlib.harness.diagnostics import PercentileCut, ThresholdCurve
from quantlib.harness.report import StrategyReport
from quantlib.harness.run import run_strategy

__all__ = [
    "HarnessConfig",
    "PercentileCut",
    "StrategyReport",
    "ThresholdCurve",
    "run_strategy",
]
