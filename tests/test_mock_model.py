"""MockMLModel tests — fully network-free. Build FeatureVectors directly and assert the model is
deterministic, varies across symbols/minutes, stays in [0, 1], and tolerates NaN feature cells."""
from __future__ import annotations

import datetime as dt

import numpy as np

from quantlib.bus.schema import default_schema
from quantlib.bus.vector import FeatureVector
from strategies.lib.model import MockMLModel

SCHEMA = default_schema()
MINUTE = dt.datetime(2026, 6, 15, 14, 30, tzinfo=dt.timezone.utc)


def _vector(symbol: str, minute: dt.datetime, fill: float = 0.5) -> FeatureVector:
    array = np.full(SCHEMA.n_features, fill, dtype="<f8")
    return FeatureVector(SCHEMA, symbol, minute, array, SCHEMA.fingerprint)


def test_probability_in_unit_interval() -> None:
    model = MockMLModel()
    for symbol in ("AAPL", "MSFT", "NVDA", "SPY", "AMD"):
        prediction = model.predict(_vector(symbol, MINUTE))
        assert 0.0 <= prediction.probability <= 1.0
        assert prediction.symbol == symbol
        assert prediction.model == "mock_ml"


def test_deterministic_same_vector() -> None:
    model = MockMLModel()
    first = model.predict(_vector("AAPL", MINUTE))
    second = model.predict(_vector("AAPL", MINUTE))
    assert first.probability == second.probability


def test_varies_across_symbols_and_minutes() -> None:
    model = MockMLModel()
    probs = {
        model.predict(_vector(symbol, MINUTE)).probability
        for symbol in ("AAPL", "MSFT", "NVDA", "SPY", "AMD")
    }
    assert len(probs) > 1  # different symbols -> different probabilities
    later = MINUTE + dt.timedelta(minutes=1)
    assert (
        model.predict(_vector("AAPL", MINUTE)).probability
        != model.predict(_vector("AAPL", later)).probability
    )


def test_folded_features_move_the_signal() -> None:
    model = MockMLModel(["ret_1m"])
    low = _vector("AAPL", MINUTE, fill=0.0)
    high = _vector("AAPL", MINUTE, fill=0.0)
    high.array[SCHEMA.offset("ret_1m")] = 0.05
    assert model.predict(low).probability != model.predict(high).probability


def test_nan_feature_does_not_crash() -> None:
    model = MockMLModel(["ret_1m"])
    vector = _vector("AAPL", MINUTE, fill=0.0)
    vector.array[SCHEMA.offset("ret_1m")] = np.nan
    prediction = model.predict(vector)
    assert 0.0 <= prediction.probability <= 1.0
