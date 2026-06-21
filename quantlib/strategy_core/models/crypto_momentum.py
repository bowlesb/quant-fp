"""A trivial crypto momentum signal behind the same ``Model.predict(vector) -> Prediction`` interface.

This is the model for the FIRST live crypto strategy container — its job is to exercise the FULL
end-to-end loop on the 24/7 crypto stream (bar -> feature vector -> strategy -> paper order), NOT to make
an edge claim. It is deliberately the simplest possible real signal: short-horizon return continuation.

The mapping (long-only, like smoke / reversion):
  - Read ``ret_{window_m}m`` (the trailing return over ``window_m`` minutes) for the configured window.
    A POSITIVE return is upward momentum, so the continuation view is UP. We map the return -> P(up) with
    a logistic in the return, scaled by ``sensitivity`` (return in raw units, e.g. 0.002 = +20 bps).
    Positive return -> probability > 0.5 (a long signal); negative -> < 0.5.
  - The strategy bets long only when ``probability > threshold``, so it only fires on names with
    sufficiently positive recent momentum.

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


class CryptoMomentumModel:
    """Long-momentum probability from ``ret_{window}m``. Positive return -> P(up) > 0.5."""

    name = "crypto_momentum"

    def __init__(self, window_m: int = 5, sensitivity: float = 200.0) -> None:
        """``window_m`` selects the ``ret_{window_m}m`` feature; ``sensitivity`` is the logistic gain on the
        (raw, not bps) return. At sensitivity 200, a +0.005 (+50 bps) return maps to P(up) ≈ 0.73; a +0.002
        (+20 bps) to ≈ 0.60; 0 to 0.50 — so the bet threshold selects how strong recent momentum must be
        before a long fires."""
        self._feature = f"ret_{window_m}m"
        self._sensitivity = sensitivity

    @property
    def feature_name(self) -> str:
        return self._feature

    def predict(self, vector: FeatureRow) -> Prediction:
        ret = vector.value(self._feature)
        if not np.isfinite(ret):
            # Warmup / sparse minute: no view. 0.5 is below any sensible long threshold, so no bet.
            return Prediction(symbol=vector.symbol, probability=0.5, model=self.name)
        # Logistic of the return: positive momentum (ret>0) -> positive argument -> P(up)>0.5.
        probability = 1.0 / (1.0 + math.exp(-self._sensitivity * ret))
        return Prediction(symbol=vector.symbol, probability=probability, model=self.name)
