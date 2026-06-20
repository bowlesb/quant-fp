"""Trainable rankers that produce a FROZEN `RankModel` — the shared object the SAME `decide` loads.

The harness fits a model walk-forward (train-on-past); the trained model is frozen into an artifact
that scores a `CrossSection` BY NAME (`rank(cs) -> per-name score`). That frozen `RankModel` is exactly
what `quantlib.strategy_core.cross_sectional_ls.CrossSectionalLS(model=...)` consumes — so the live
container loads the frozen model and calls `core.decide` per cycle with ZERO re-implementation. The
walk-forward fold orchestration is backtest-only (it PRODUCES the frozen model); `rank` is the shared
code path (mirrors the feature platform: backtest-only fold orchestration, one shared `emit`).

Three model kinds (config-driven):
  - GBM    — LightGBM gradient-boosted trees (robust default; NaN-tolerant; non-linear combiner).
  - RIDGE  — a regularized linear model over per-fold-standardized features (fast, interpretable).
  - COMPOSITE — the no-fit equal-weight z-score composite (the battery fast-path screen; no labels).

All three read the SAME `feature_names` BY NAME off the cross-section — the name is the invariant that
makes the harness apply == the live apply.
"""
from __future__ import annotations

import numpy as np

import lightgbm as lgb

from quantlib.strategy_core import CrossSection

# A robust, fast LightGBM default (the battery's DEFAULT_LGB shape — shallow trees, modest rounds).
_LGB_PARAMS: dict[str, object] = {
    "objective": "regression",
    "num_leaves": 31,
    "learning_rate": 0.05,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "min_data_in_leaf": 50,
    "verbosity": -1,
}
_LGB_ROUNDS = 200


def _feature_matrix(cross_section: CrossSection, feature_names: list[str]) -> np.ndarray:
    """Gather the (n_names, n_features) matrix from the cross-section BY NAME (NaN where absent) — the
    SAME by-name read in the harness (panel slice) and live (bus vector)."""
    cols = [np.asarray(cross_section.feature(name), dtype=float) for name in feature_names]
    return np.column_stack(cols) if cols else np.empty((len(cross_section.symbols), 0))


class GbmRankModel:
    """A FROZEN LightGBM ranker. `rank(cs)` reads the feature matrix BY NAME and returns the booster's
    per-name prediction (higher = more bullish forward return). NaN-tolerant (LightGBM native)."""

    def __init__(self, booster: lgb.Booster, feature_names: list[str]) -> None:
        self._booster = booster
        self._feature_names = feature_names

    def rank(self, cross_section: CrossSection) -> np.ndarray:
        matrix = _feature_matrix(cross_section, self._feature_names)
        if matrix.shape[0] == 0:
            return np.empty(0)
        return np.asarray(self._booster.predict(matrix), dtype=float)

    @classmethod
    def train(cls, train_x: np.ndarray, train_y: np.ndarray, feature_names: list[str]) -> "GbmRankModel":
        dataset = lgb.Dataset(train_x, label=train_y)
        booster = lgb.train(_LGB_PARAMS, dataset, num_boost_round=_LGB_ROUNDS)
        return cls(booster, feature_names)


class RidgeRankModel:
    """A FROZEN regularized-linear ranker. Features are standardized with the TRAIN fold's mean/std
    (no test leakage; NaN -> 0 after standardization), then scored by the fitted ridge weights. Pure
    numpy (closed-form ridge) so it is dependency-light and exactly reproducible."""

    def __init__(
        self,
        weights: np.ndarray,
        intercept: float,
        mean: np.ndarray,
        std: np.ndarray,
        feature_names: list[str],
    ) -> None:
        self._weights = weights
        self._intercept = intercept
        self._mean = mean
        self._std = std
        self._feature_names = feature_names

    def _standardize(self, matrix: np.ndarray) -> np.ndarray:
        standardized = (matrix - self._mean) / self._std
        return np.where(np.isfinite(standardized), standardized, 0.0)

    def rank(self, cross_section: CrossSection) -> np.ndarray:
        matrix = _feature_matrix(cross_section, self._feature_names)
        if matrix.shape[0] == 0:
            return np.empty(0)
        return self._standardize(matrix) @ self._weights + self._intercept

    @classmethod
    def train(
        cls,
        train_x: np.ndarray,
        train_y: np.ndarray,
        feature_names: list[str],
        *,
        ridge_lambda: float = 1.0,
    ) -> "RidgeRankModel":
        mean = np.nanmean(train_x, axis=0)
        std = np.nanstd(train_x, axis=0)
        std = np.where(std > 0, std, 1.0)
        standardized = np.where(np.isfinite((train_x - mean) / std), (train_x - mean) / std, 0.0)
        target = np.where(np.isfinite(train_y), train_y, 0.0)
        n_features = standardized.shape[1]
        gram = standardized.T @ standardized + ridge_lambda * np.eye(n_features)
        weights = np.linalg.solve(gram, standardized.T @ target)
        intercept = float(np.mean(target))
        return cls(weights, intercept, mean, std, feature_names)


class CompositeRankModel:
    """The no-fit equal-weight z-score composite (the battery fast-path screen). Features standardized
    by the TRAIN fold's mean/std, the per-name composite is the mean z across columns. No labels used —
    a directional-naive whole-set screen (the honest "does ANY equal-weight combination rank?")."""

    def __init__(self, mean: np.ndarray, std: np.ndarray, feature_names: list[str]) -> None:
        self._mean = mean
        self._std = std
        self._feature_names = feature_names

    def rank(self, cross_section: CrossSection) -> np.ndarray:
        matrix = _feature_matrix(cross_section, self._feature_names)
        if matrix.shape[0] == 0:
            return np.empty(0)
        standardized = (matrix - self._mean) / self._std
        composite = np.nanmean(np.where(np.isfinite(standardized), standardized, np.nan), axis=1)
        return np.where(np.isfinite(composite), composite, 0.0)

    @classmethod
    def train(
        cls, train_x: np.ndarray, train_y: np.ndarray, feature_names: list[str]
    ) -> "CompositeRankModel":
        mean = np.nanmean(train_x, axis=0)
        std = np.nanstd(train_x, axis=0)
        std = np.where(std > 0, std, 1.0)
        return cls(mean, std, feature_names)
