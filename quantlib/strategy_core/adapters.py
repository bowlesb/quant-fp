"""The two `CrossSection` adapters — the ONLY thing that differs between backtest and live.

`PanelCrossSection` wraps one timestamp-slice of the battery's column-major `Panel`.
`BusCrossSection` wraps the live container's latest-by-symbol decoded `FeatureVector` dict.

Both expose the identical `CrossSection` interface (named, NaN-safe per-name feature reads), so the
SAME `DecisionCore.decide` runs over either with zero re-code. These adapters are ~15 lines each — the
entire backtest-vs-live difference at the decision layer.
"""
from __future__ import annotations

import datetime as dt

import numpy as np


class PanelCrossSection:
    """One timestamp's cross-section view onto the battery `Panel` (the backtest side).

    `row_index` are the panel rows belonging to this minute; `feature_columns` maps feature name ->
    column in the panel's feature_matrix. Per-name reads are O(1) numpy gathers over the resident
    arrays — no copy of the panel, no store re-read."""

    def __init__(
        self,
        symbols: list[str],
        minute: dt.datetime,
        feature_matrix_slice: np.ndarray,  # (n_names, n_features) for this minute
        feature_columns: dict[str, int],
        extra_columns: dict[str, np.ndarray] | None = None,
    ) -> None:
        self.symbols = symbols
        self.minute = minute
        self._matrix = feature_matrix_slice
        self._cols = feature_columns
        # execution columns (entry_close, half_spread_bps, ...) readable by name alongside features,
        # so the BacktestExecutor reads fill price + cost through the SAME by-name interface as live.
        self._extra = extra_columns or {}
        self._sym_index = {symbol: i for i, symbol in enumerate(symbols)}

    def feature(self, name: str) -> np.ndarray:
        if name in self._cols:
            return self._matrix[:, self._cols[name]]
        if name in self._extra:
            return self._extra[name]
        return np.full(len(self.symbols), np.nan)

    def feature_for(self, symbol: str, name: str) -> float:
        if symbol not in self._sym_index:
            return float("nan")
        if name in self._cols:
            return float(self._matrix[self._sym_index[symbol], self._cols[name]])
        if name in self._extra:
            return float(self._extra[name][self._sym_index[symbol]])
        return float("nan")


class BusCrossSection:
    """The cross-section view onto the live bus (the live container side): the latest decoded
    `FeatureVector` per symbol. Per-name reads use the vector's own O(1) name->offset accessor — the
    SAME by-name addressing the backtest uses, so the decision reads identical inputs both sides.

    `latest_by_symbol` is exactly what `ReversionStrategy._latest_by_symbol` already maintains."""

    def __init__(self, latest_by_symbol: dict[str, object]) -> None:
        # value type is quantlib.bus.vector.FeatureVector; typed as object to avoid importing the bus
        # (and its redis/schema deps) into the pure core — duck-typed on `.value(name)` / `.minute`.
        self._latest = latest_by_symbol
        self.symbols = list(latest_by_symbol)
        minutes = [getattr(vector, "minute", None) for vector in latest_by_symbol.values()]
        valid = [minute for minute in minutes if minute is not None]
        self.minute = max(valid) if valid else dt.datetime.now(dt.timezone.utc)

    def feature(self, name: str) -> np.ndarray:
        return np.array([self._read(self._latest[symbol], name) for symbol in self.symbols], dtype=float)

    def feature_for(self, symbol: str, name: str) -> float:
        if symbol not in self._latest:
            return float("nan")
        return self._read(self._latest[symbol], name)

    @staticmethod
    def _read(vector: object, name: str) -> float:
        value = vector.value(name)  # type: ignore[attr-defined]
        return float(value)
