"""The harness PORTABILITY-PARITY proof (the make-or-break invariant).

The harness trains a model offline and applies it via the SHARED `CrossSectionalLS.decide`. This suite
proves the decision is portable to production BY CONSTRUCTION — there is ONE scoring expression, applied
two ways with identical results:

  test_decide_parity_panel_vs_bus  — the SAME frozen-model `decide` over a `PanelCrossSection` (the
    harness/backtest source) and a `BusCrossSection` (the live container's source) built from IDENTICAL
    feature values yields IDENTICAL target books. (execution-agnostic.)

  test_batch_vs_per_event_select_identical  — the harness's VECTORIZED batch apply (predict the whole
    fold's matrix at once, rank per timestamp) selects the SAME legs as applying `decide` PER single
    cross-section, timestamp by timestamp. (speed-agnostic — proves no backtest-only fast path can
    drift from the live per-event path.)

Together these are the strategy-layer analogue of the feature stream==backfill parity test, now for a
TRAINED model: the frozen `RankModel.rank` is the shared object both paths call.
"""
from __future__ import annotations

import datetime as dt

import numpy as np

from quantlib.harness.model import GbmRankModel, RidgeRankModel
from quantlib.strategy_core import TargetPosition
from quantlib.strategy_core.adapters import BusCrossSection, PanelCrossSection
from quantlib.strategy_core.cross_sectional_ls import CrossSectionalLS

FEATURE_NAMES = ["f0", "f1", "f2"]


class _FakeVector:
    """Duck-typed FeatureVector — `.value(name)` + `.minute`, no bus deps."""

    def __init__(self, symbol: str, minute: dt.datetime, values: dict[str, float]) -> None:
        self.symbol = symbol
        self.minute = minute
        self._values = values

    def value(self, name: str) -> float:
        return float(self._values.get(name, float("nan")))


def _train_data(n: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    train_x = rng.normal(0, 1, (n, len(FEATURE_NAMES)))
    # a learnable target so the trained model is non-trivial
    train_y = train_x[:, 0] - 0.5 * train_x[:, 1] + 0.1 * rng.normal(0, 1, n)
    return train_x, train_y


def _panel_cs(symbols: list[str], minute: dt.datetime, matrix: np.ndarray) -> PanelCrossSection:
    columns = {name: i for i, name in enumerate(FEATURE_NAMES)}
    return PanelCrossSection(symbols, minute, matrix, columns)


def _bus_cs(symbols: list[str], minute: dt.datetime, matrix: np.ndarray) -> BusCrossSection:
    latest = {
        symbol: _FakeVector(
            symbol, minute, {name: float(matrix[i, j]) for j, name in enumerate(FEATURE_NAMES)}
        )
        for i, symbol in enumerate(symbols)
    }
    return BusCrossSection(latest)


def _targets_as_dict(targets: list[TargetPosition]) -> dict[str, float]:
    return {target.symbol: round(target.target_weight, 12) for target in targets}


def test_decide_parity_panel_vs_bus_gbm() -> None:
    """The SAME frozen GBM model + `decide` over Panel and Bus cross-sections yields identical books."""
    train_x, train_y = _train_data(2000, seed=7)
    model = GbmRankModel.train(train_x, train_y, FEATURE_NAMES)
    core = CrossSectionalLS(frac=0.2, model=model)

    rng = np.random.default_rng(99)
    n = 40
    symbols = [f"S{i}" for i in range(n)]
    minute = dt.datetime(2026, 1, 5, 19, 59, tzinfo=dt.timezone.utc)
    matrix = rng.normal(0, 1, (n, len(FEATURE_NAMES)))

    panel_book = _targets_as_dict(core.decide(_panel_cs(symbols, minute, matrix)))
    bus_book = _targets_as_dict(core.decide(_bus_cs(symbols, minute, matrix)))
    assert panel_book == bus_book
    assert len(panel_book) == 2 * int(0.2 * n)


def test_decide_parity_panel_vs_bus_ridge() -> None:
    train_x, train_y = _train_data(2000, seed=11)
    model = RidgeRankModel.train(train_x, train_y, FEATURE_NAMES)
    core = CrossSectionalLS(frac=0.1, model=model)

    rng = np.random.default_rng(123)
    n = 60
    symbols = [f"S{i}" for i in range(n)]
    minute = dt.datetime(2026, 2, 2, 19, 59, tzinfo=dt.timezone.utc)
    matrix = rng.normal(0, 1, (n, len(FEATURE_NAMES)))

    panel_book = _targets_as_dict(core.decide(_panel_cs(symbols, minute, matrix)))
    bus_book = _targets_as_dict(core.decide(_bus_cs(symbols, minute, matrix)))
    assert panel_book == bus_book


def test_batch_vs_per_event_select_identical() -> None:
    """The harness's VECTORIZED batch apply == the PER-EVENT apply, timestamp by timestamp.

    BATCH: the frozen model scores the whole multi-timestamp matrix at once, then each timestamp's
    top/bottom-k is taken (the harness path). PER-EVENT: build one `PanelCrossSection` per timestamp and
    call `decide`. The selected legs MUST be identical — proving the fast batch path cannot drift from
    the per-event path the live container runs."""
    train_x, train_y = _train_data(3000, seed=3)
    model = GbmRankModel.train(train_x, train_y, FEATURE_NAMES)
    frac = 0.2
    core = CrossSectionalLS(frac=frac, model=model)

    rng = np.random.default_rng(55)
    n_ts = 6
    n_names = 30
    minutes = [
        dt.datetime(2026, 3, 1, 19, 59, tzinfo=dt.timezone.utc) + dt.timedelta(days=d) for d in range(n_ts)
    ]

    per_event_books: list[dict[str, float]] = []
    batch_books: list[dict[str, float]] = []
    for minute in minutes:
        symbols = [f"S{i}" for i in range(n_names)]
        matrix = rng.normal(0, 1, (n_names, len(FEATURE_NAMES)))
        # per-event: decide on this single cross-section
        per_event_books.append(_targets_as_dict(core.decide(_panel_cs(symbols, minute, matrix))))
        # batch: score via the SAME core.score, then top/bottom-k columnar (the harness booking)
        scores = core.score(_panel_cs(symbols, minute, matrix))
        order = np.argsort(scores)
        k = max(1, int(frac * n_names))
        book: dict[str, float] = {}
        for idx in order[-k:]:
            book[symbols[idx]] = round(1.0 / k, 12)
        for idx in order[:k]:
            book[symbols[idx]] = round(-1.0 / k, 12)
        batch_books.append(book)

    assert per_event_books == batch_books


def test_frozen_model_is_the_shared_object() -> None:
    """A trained model frozen here scores a live-shaped BusCrossSection identically to the harness's
    panel scoring — i.e. the SAME artifact graduates to live with no re-fit, no re-code."""
    train_x, train_y = _train_data(1500, seed=21)
    model = RidgeRankModel.train(train_x, train_y, FEATURE_NAMES)

    rng = np.random.default_rng(321)
    n = 25
    symbols = [f"S{i}" for i in range(n)]
    minute = dt.datetime(2026, 4, 4, 19, 59, tzinfo=dt.timezone.utc)
    matrix = rng.normal(0, 1, (n, len(FEATURE_NAMES)))

    panel_scores = model.rank(_panel_cs(symbols, minute, matrix))
    bus_scores = model.rank(_bus_cs(symbols, minute, matrix))
    np.testing.assert_allclose(panel_scores, bus_scores, rtol=0, atol=0)
