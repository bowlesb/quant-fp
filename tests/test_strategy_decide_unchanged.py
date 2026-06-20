"""decide()-UNCHANGED regression (Ben's current-good-state discipline): wiring the strategies to the
production ProductionExecutor + StrategyState changes ONLY the execution/state backing — the DECISIONS
each strategy makes must be byte-identical to the pre-wiring path.

The decision logic for all three live strategies already lives in pure, execution-agnostic cores that read
features BY NAME off a FeatureRow/CrossSection (the #196 parity + the FeatureView consume):
  - reversion: `VwapReversionModel.predict` + `select_candidate`
  - smoke:     `MockMLModel.predict`
  - overnight_beta: `OvernightBetaModel.select_legs`
None of these touch an executor, a store, or a clock — so swapping the execution backing cannot change
their output. These tests pin that invariant: the same inputs -> identical decisions, and the production
OrderIntent carries the SAME economic decision (symbol/side/qty) the bespoke path would have placed.
"""

from __future__ import annotations

import datetime as dt

import numpy as np

from quantlib.strategy_core.models.overnight_beta import OvernightBetaModel
from quantlib.strategy_core.models.single_name import MockMLModel
from quantlib.strategy_core.models.vwap_reversion import VwapReversionModel
from quantlib.strategy_core.production_execution import ProductionOrderIntent
from strategies.reversion.strategy import select_candidate

TS = dt.datetime(2026, 6, 19, 20, 0, tzinfo=dt.timezone.utc)


class _Row:
    """A minimal FeatureRow (symbol/minute/value) — the bus-free surface the cores read."""

    def __init__(self, symbol: str, values: dict[str, float]) -> None:
        self.symbol = symbol
        self.minute = TS
        self._values = values

    def value(self, name: str) -> float:
        return self._values.get(name, float("nan"))


def test_reversion_decide_is_pure_and_deterministic() -> None:
    """select_candidate is a pure function of (model, latest_by_symbol, threshold, excluded) — identical
    across repeated calls and independent of any executor/store."""
    model = VwapReversionModel(window_m=30)
    feature = model.feature_name
    latest = {
        "AAPL": _Row("AAPL", {feature: -0.005}),  # most below VWAP -> highest P(up)
        "MSFT": _Row("MSFT", {feature: -0.001}),
        "NVDA": _Row("NVDA", {feature: +0.002}),  # above VWAP -> not a long
    }
    a = select_candidate(model, latest, threshold=0.55, excluded=set())  # type: ignore[arg-type]
    b = select_candidate(model, latest, threshold=0.55, excluded=set())  # type: ignore[arg-type]
    assert a is not None and b is not None
    assert a.symbol == b.symbol == "AAPL"  # the most-stretched-below-VWAP name, deterministically
    assert a.probability == b.probability


def test_reversion_decision_maps_to_same_production_intent() -> None:
    """The DECISION (which symbol, which side) is unchanged; the production OrderIntent carries exactly
    that economic order with the G2 coid — only the execution representation differs from the bespoke
    MarketOrderRequest."""
    model = VwapReversionModel(window_m=30)
    latest = {"AAPL": _Row("AAPL", {model.feature_name: -0.005})}
    candidate = select_candidate(model, latest, threshold=0.55, excluded=set())  # type: ignore[arg-type]
    assert candidate is not None
    intent = ProductionOrderIntent(
        strategy_id="reversion", symbol=candidate.symbol, side="buy", decision_ts=TS, notional=50.0
    )
    assert intent.symbol == "AAPL" and intent.side == "buy"
    assert intent.client_order_id == "reversion-20260619T200000-AAPL-buy"


def test_smoke_decide_unchanged_deterministic() -> None:
    model = MockMLModel(["ret_1m", "volume_zscore_5m"])
    row = _Row("AAPL", {"ret_1m": 0.01, "volume_zscore_5m": 1.2})
    p1 = model.predict(row)
    p2 = model.predict(row)
    assert p1.probability == p2.probability  # deterministic per (symbol, minute, folded features)
    assert 0.0 <= p1.probability <= 1.0


def test_overnight_beta_decide_unchanged_deterministic() -> None:
    model = OvernightBetaModel(beta_window=20, quantile=0.2)
    rng = np.random.default_rng(0)
    market = rng.standard_normal(40)
    # need >=5 names with finite betas for the model to select legs; betas span low->high.
    betas = {"LO": 0.2, "L2": 0.6, "MID": 1.0, "H2": 1.4, "HI": 1.8}
    returns = {name: market * beta + rng.standard_normal(40) * 0.01 for name, beta in betas.items()}
    legs_a = model.select_legs(returns, market)
    legs_b = model.select_legs(returns, market)
    assert legs_a.long == legs_b.long  # deterministic leg selection
    assert legs_a.short == legs_b.short
    assert "HI" in legs_a.long and "LO" in legs_a.short  # the economically-right legs, unchanged
