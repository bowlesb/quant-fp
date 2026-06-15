"""Data loader written against the FeatureStoreClient CONTRACT (docs/MODELLING_BOUNDARY.md).

The real platform exposes ``quantlib.modelling.client.FeatureStoreClient`` with:
    catalog() -> pl.DataFrame
    get_features(names, symbols, start, end, source) -> pl.DataFrame   # tidy (symbol, minute) x feature
    training_export(names, start, end, min_grade) -> pl.DataFrame      # certified-only, settled

This module defines ``SyntheticFeatureStoreClient`` with the SAME signatures, backed by the synthetic
panel. A model trains against this interface, so when real data arrives the swap is one line:

    # synthetic (now):
    client = SyntheticFeatureStoreClient.from_parquet("synth_panel.parquet")
    # real (later):
    from quantlib.modelling.client import FeatureStoreClient
    client = FeatureStoreClient(store_root=..., val_root=...)

Everything downstream (panel_to_arrays, the walk-forward splitter, the model) consumes only
catalog() + get_features()/training_export() and never touches platform internals.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
FEATURE_META = HERE / "feature_meta.json"

# Labels live in the panel for the synthetic case. With the REAL client, labels are constructed
# separately from forward returns (see NOTES.md "label construction") -- they are NOT features.
LABEL_COLS = ["fwd_ret_5m", "fwd_ret_15m", "fwd_ret_30m", "fwd_ret_60m"]
HORIZONS = [5, 15, 30, 60]
ID_COLS = ["minute", "symbol"]


@dataclass
class SyntheticFeatureStoreClient:
    """Mirror of FeatureStoreClient backed by an in-memory synthetic panel."""

    panel: pl.DataFrame
    _catalog: pl.DataFrame

    @classmethod
    def from_parquet(cls, path: str | Path) -> "SyntheticFeatureStoreClient":
        panel = pl.read_parquet(path)
        meta = json.loads(FEATURE_META.read_text())
        catalog = pl.DataFrame(meta).with_columns(
            pl.lit("certified").alias("status"),
            pl.lit("A").alias("value_grade"),
        )
        return cls(panel=panel, _catalog=catalog)

    def catalog(self) -> pl.DataFrame:
        """Every feature with its meaning + (synthetic) trust grade -- same shape as the real catalog()."""
        return self._catalog

    def feature_names(self) -> list[str]:
        return [name for name in self.panel.columns if name not in ID_COLS and name not in LABEL_COLS]

    def get_features(
        self,
        names: list[str],
        symbols: list[str] | str,
        start: dt.datetime,
        end: dt.datetime,
        source: str = "auto",
    ) -> pl.DataFrame:
        """Tidy (symbol, minute) x feature frame -- same signature as FeatureStoreClient.get_features."""
        frame = self.panel.filter((pl.col("minute") >= start) & (pl.col("minute") < end))
        if symbols != "universe" and not isinstance(symbols, str):
            frame = frame.filter(pl.col("symbol").is_in(symbols))
        keep = ID_COLS + [name for name in names if name in frame.columns]
        # include labels if explicitly requested (real client would source these from forward returns)
        keep += [name for name in names if name in LABEL_COLS and name not in keep]
        return frame.select([col for col in keep if col in frame.columns])

    def training_export(
        self,
        names: list[str],
        start: dt.datetime,
        end: dt.datetime,
        min_grade: str = "B",
    ) -> pl.DataFrame:
        """Certified-only, settled training set -- same signature as the real training_export.
        For synthetic data all features are 'certified' so this returns features + labels."""
        frame = self.panel.filter((pl.col("minute") >= start) & (pl.col("minute") < end))
        keep = ID_COLS + names + LABEL_COLS
        return frame.select([col for col in keep if col in frame.columns])


@dataclass
class PanelArrays:
    """Dense arrays for modelling, indexed by a stable (minute, symbol) order.

    features: (n_rows, n_feat) float32
    labels:   (n_rows, n_horizon) float32   (cross-sectionally meaningful forward returns)
    minute_index: (n_rows,) int    -- contiguous minute id, monotonic; the time axis for walk-forward
    symbol_index: (n_rows,) int
    """

    features: np.ndarray
    labels: np.ndarray
    minute_index: np.ndarray
    symbol_index: np.ndarray
    feature_names: list[str]
    label_names: list[str]
    minute_values: np.ndarray  # datetime64, one per unique minute id

    @property
    def n_minutes(self) -> int:
        return int(self.minute_index.max()) + 1


def panel_to_arrays(frame: pl.DataFrame, feature_names: list[str]) -> PanelArrays:
    """Convert a tidy (symbol, minute) x feature frame (the client's output) into dense arrays."""
    frame = frame.sort(["minute", "symbol"])
    unique_minutes = frame.select("minute").unique(maintain_order=True).to_series()
    minute_to_id = {val: idx for idx, val in enumerate(unique_minutes.to_list())}
    unique_symbols = frame.select("symbol").unique(maintain_order=True).to_series().to_list()
    symbol_to_id = {sym: idx for idx, sym in enumerate(unique_symbols)}

    minute_index = np.array([minute_to_id[val] for val in frame["minute"].to_list()], dtype=np.int64)
    symbol_index = np.array([symbol_to_id[val] for val in frame["symbol"].to_list()], dtype=np.int64)

    features = frame.select(feature_names).to_numpy().astype(np.float32)
    label_names = [name for name in LABEL_COLS if name in frame.columns]
    labels = frame.select(label_names).to_numpy().astype(np.float32)

    return PanelArrays(
        features=features,
        labels=labels,
        minute_index=minute_index,
        symbol_index=symbol_index,
        feature_names=feature_names,
        label_names=label_names,
        minute_values=unique_minutes.to_numpy(),
    )
