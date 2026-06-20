"""A VWAP mean-reversion signal behind the same ``Model.predict(vector) -> Prediction`` interface.

This is the FIRST non-mock model: a deliberately simple, transparent, parameter-light reversion signal
grounded in the platform's strongest historically-observed price predictor — ``vwap_deviation_{w}m``
(close/VWAP − 1) mean-reverts intraday (``docs/EXPERIMENTS.md``: the 30m signal is vwap_dev reversion,
linear, model-independent). It is NOT a fitted ML model and makes no edge claim net-of-cost (the standing
verdict is vwap_dev is REAL-but-uneconomic at minute turnover); it exists to prove the MULTI-STRATEGY
design with a real ``predict``-driven container that trades a real (if modest) signal, paper-only.

The mapping (long-only, like smoke):
  - Read ``vwap_deviation_{w}m`` for the configured window ``w``. A NEGATIVE deviation means the price is
    BELOW its trailing VWAP — stretched down — so the reversion view is UP. We map deviation -> P(up over
    the horizon) with a logistic in the deviation, scaled by ``sensitivity`` (deviation in raw units, e.g.
    −0.002 = −20 bps below VWAP). Below VWAP -> probability > 0.5 (a long signal); above VWAP -> < 0.5.
  - The strategy bets long only when ``probability > threshold``, so it only fires on names stretched
    sufficiently BELOW VWAP — exactly the reversion-buy setup.

NaN-safe and point-in-time by construction: it reads ONLY the named feature off the decoded vector (a
value the producer computed point-in-time as of that minute) and returns 0.5 (no signal) when the feature
is non-finite (warmup / sparse), so it can never crash or bet on a missing input. Deterministic: the same
vector always yields the same probability — reproducible in tests, no wall-clock, no RNG.
"""
from __future__ import annotations

import math

import numpy as np

from quantlib.strategy_core.feature_row import FeatureRow
from quantlib.strategy_core.models.single_name import Prediction


class VwapReversionModel:
    """Long-reversion probability from ``vwap_deviation_{window}m``. Below-VWAP -> P(up) > 0.5."""

    name = "vwap_reversion"

    def __init__(self, window_m: int = 30, sensitivity: float = 400.0) -> None:
        """``window_m`` selects the ``vwap_deviation_{window_m}m`` feature; ``sensitivity`` is the logistic
        gain on the (raw, not bps) deviation. At sensitivity 400, a −0.005 (−50 bps) deviation maps to
        P(up) ≈ 0.88; a −0.001 (−10 bps) to ≈ 0.60; 0 to 0.50 — so the bet threshold selects how far below
        VWAP a name must be stretched before a long fires."""
        self._feature = f"vwap_deviation_{window_m}m"
        self._sensitivity = sensitivity

    @property
    def feature_name(self) -> str:
        return self._feature

    def predict(self, vector: FeatureRow) -> Prediction:
        deviation = vector.value(self._feature)
        if not np.isfinite(deviation):
            # Warmup / sparse minute: no view. 0.5 is below any sensible long threshold, so no bet.
            return Prediction(symbol=vector.symbol, probability=0.5, model=self.name)
        # Logistic of the NEGATED deviation: below VWAP (deviation<0) -> positive argument -> P(up)>0.5.
        probability = 1.0 / (1.0 + math.exp(self._sensitivity * deviation))
        return Prediction(symbol=vector.symbol, probability=probability, model=self.name)
