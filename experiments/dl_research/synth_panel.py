"""Synthesize a realistic cross-sectional equity panel for DL-harness validation.

Generates a (minute x symbol x feature) panel using the PLATFORM'S REAL 519 feature names
(read from feature_meta.json, exported from quantlib.features.registry.REGISTRY.catalog()) with:

  - a PLANTED cross-sectional signal: a small handful of "real" features genuinely predict the
    forward returns; the other ~510 are noise. The signal is CROSS-SECTIONAL (it sets each stock's
    return RELATIVE to its peers at the same minute), which is exactly the edge a univariate
    time-series model cannot see and a cross-sectional ranker must recover.
  - SECTOR structure: a sector factor that loads onto returns (sector one-hot features are populated
    truthfully so sector membership is learnable).
  - a REGIME variable: a slow market state that FLIPS THE SIGN / weight of the predictive features
    (the MASTER "market-guided gating" motivation) -- a model that ignores regime leaves IC on the
    table.
  - 4 forward-return labels: fwd_ret_5m / 15m / 30m / 60m, cross-sectionally meaningful, low-SNR.

The output schema deliberately matches what FeatureStoreClient.get_features(...) returns: a tidy
(symbol, minute) x feature polars frame. SyntheticFeatureStoreClient (loader.py) serves it through the
SAME signature as the real client, so swapping synthetic->real is a one-line change.

This is RESEARCH scratch. It does not touch platform code.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl

HERE = Path(__file__).resolve().parent
FEATURE_META = HERE / "feature_meta.json"

SECTOR_FEATURES = [
    "sector_is_technology", "sector_is_healthcare", "sector_is_financial_services",
    "sector_is_consumer_cyclical", "sector_is_consumer_defensive", "sector_is_industrials",
    "sector_is_energy", "sector_is_basic_materials", "sector_is_real_estate",
    "sector_is_utilities", "sector_is_communication_services",
]

# Real feature names we hijack as the genuinely-predictive ones. Chosen to be plausible alpha sources
# (momentum, reversal, volume, volatility) per Gu/Kelly/Xiu's dominant signals.
SIGNAL_FEATURES = [
    "return_rank_15m",      # cross-sectional momentum rank (already a CS feature in the registry)
    "rsi_14m",              # mean-reversion / momentum
    "dollar_volume_rank_1m",  # liquidity
    "ret_skew_15m",         # asymmetry
    "dist_from_vwap_5d",    # reversion to vwap
]

LABELS = ["fwd_ret_5m", "fwd_ret_15m", "fwd_ret_30m", "fwd_ret_60m"]
HORIZONS = [5, 15, 30, 60]

LABEL_COLS = LABELS  # public alias


@dataclass
class PanelConfig:
    n_symbols: int = 500
    n_minutes: int = 2000
    n_sectors: int = 11
    seed: int = 17
    # SNR knobs -- finance is low-SNR (Gu/Kelly/Xiu: monthly R^2 ~0.3-0.4%). Keep the signal small.
    signal_strength: float = 0.35   # fraction of label variance explained by the planted signal (per horizon, regime-avg)
    sector_strength: float = 0.15   # fraction explained by the sector factor
    regime_period: int = 400        # minutes per regime half-cycle (slow market state)
    feature_names: list[str] = field(default_factory=list)


def load_feature_names() -> list[str]:
    meta = json.loads(FEATURE_META.read_text())
    return [row["feature"] for row in meta]


def _assign_sectors(n_symbols: int, n_sectors: int, rng: np.random.Generator) -> np.ndarray:
    return rng.integers(0, n_sectors, size=n_symbols)


def _build_regime(n_minutes: int, period: int) -> np.ndarray:
    """A slow market-state in [-1, 1]: a smooth sign that flips ~every `period` minutes."""
    time_axis = np.arange(n_minutes)
    return np.sin(2.0 * np.pi * time_axis / (2.0 * period))


def generate_panel(config: PanelConfig) -> pl.DataFrame:
    """Return a tidy (symbol, minute) x [519 features + 4 labels] polars frame with a planted CS signal."""
    rng = np.random.default_rng(config.seed)
    feature_names = config.feature_names or load_feature_names()
    n_sym, n_min = config.n_symbols, config.n_minutes

    symbols = [f"SYN{idx:04d}" for idx in range(n_sym)]
    base_time = datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc)
    minutes = [base_time + timedelta(minutes=step) for step in range(n_min)]

    sector_of = _assign_sectors(n_sym, config.n_sectors, rng)
    regime = _build_regime(n_min, config.regime_period)  # (n_min,)

    # ---- noise features: standard-normal-ish, the ~510 that carry no signal ----
    # shape (n_min, n_sym, n_feat); generated per-feature so memory stays bounded.
    feature_idx = {name: pos for pos, name in enumerate(feature_names)}
    n_feat = len(feature_names)
    panel = rng.standard_normal((n_min, n_sym, n_feat)).astype(np.float32) * 0.5

    # ---- sector one-hots: populate truthfully ----
    for sector_pos, sector_feat in enumerate(SECTOR_FEATURES[: config.n_sectors]):
        col = feature_idx[sector_feat]
        onehot = (sector_of == sector_pos).astype(np.float32)
        panel[:, :, col] = onehot[None, :]

    # ---- the genuinely-predictive features: give them real cross-sectional structure ----
    # Each signal feature = a persistent per-(minute,symbol) factor (AR(1) over time) so it looks like
    # a real slowly-evolving feature, not white noise.
    signal_cols = [feature_idx[name] for name in SIGNAL_FEATURES]
    signal_values = np.empty((n_min, n_sym, len(SIGNAL_FEATURES)), dtype=np.float32)
    for sig_pos in range(len(SIGNAL_FEATURES)):
        series = np.empty((n_min, n_sym), dtype=np.float32)
        series[0] = rng.standard_normal(n_sym)
        ar_rho = 0.95
        innov_scale = np.sqrt(1.0 - ar_rho**2)
        for step in range(1, n_min):
            series[step] = ar_rho * series[step - 1] + innov_scale * rng.standard_normal(n_sym)
        # cross-sectionally standardize each minute (features are cross-sectional in spirit)
        series -= series.mean(axis=1, keepdims=True)
        series /= series.std(axis=1, keepdims=True) + 1e-8
        signal_values[:, :, sig_pos] = series
        panel[:, :, signal_cols[sig_pos]] = series

    # sector factor: a per-(minute,sector) shock that loads on returns
    sector_factor_min = rng.standard_normal((n_min, config.n_sectors)).astype(np.float32)
    sector_factor = sector_factor_min[:, sector_of]  # (n_min, n_sym)

    # ---- planted regression: forward returns = signal . beta(regime) + sector + noise ----
    # beta per signal feature; the regime MODULATES the sign/strength of two of them.
    base_beta = np.array([1.0, -0.6, 0.5, 0.4, -0.7], dtype=np.float32)
    labels = {}
    for horizon in HORIZONS:
        # regime-modulated betas: features 0 and 3 flip with the regime (state-dependent alpha)
        # regime is (n_min,) -> broadcast to (n_min, n_sym)
        regime_min = regime[:, None]  # (n_min, 1)
        beta_mod = np.tile(base_beta, (n_min, 1)).astype(np.float32)  # (n_min, n_feat_sig)
        beta_mod[:, 0] = base_beta[0] * regime  # sign flips with regime
        beta_mod[:, 3] = base_beta[3] * (0.5 + 0.5 * regime)  # strength gated by regime
        # longer horizons: slightly stronger signal, more accumulated noise (typical of forward rets)
        horizon_scale = np.sqrt(horizon / 15.0).astype(np.float32)

        # signal contribution: sum_f signal[:,:,f] * beta_mod[:, f]
        signal_contrib = np.einsum("msf,mf->ms", signal_values, beta_mod).astype(np.float32)
        signal_contrib *= horizon_scale
        # standardize the signal contribution cross-sectionally per minute
        signal_contrib -= signal_contrib.mean(axis=1, keepdims=True)
        signal_contrib /= signal_contrib.std(axis=1, keepdims=True) + 1e-8

        sector_contrib = sector_factor - sector_factor.mean(axis=1, keepdims=True)
        sector_contrib /= sector_contrib.std(axis=1, keepdims=True) + 1e-8

        noise = rng.standard_normal((n_min, n_sym)).astype(np.float32)

        sig_w = np.sqrt(config.signal_strength)
        sec_w = np.sqrt(config.sector_strength)
        noise_w = np.sqrt(max(1e-6, 1.0 - config.signal_strength - config.sector_strength))
        ret = sig_w * signal_contrib + sec_w * sector_contrib + noise_w * noise
        # scale to realistic intraday return magnitude (~10-40 bps std depending on horizon)
        ret *= 0.001 * horizon_scale
        labels[f"fwd_ret_{horizon}m"] = ret
        _ = regime_min, beta_mod  # silence unused in some branches

    # ---- flatten (n_min, n_sym, *) -> tidy long frame ----
    minute_col = np.repeat(np.arange(n_min), n_sym)
    symbol_col = np.tile(np.arange(n_sym), n_min)
    frame_data: dict[str, object] = {
        "minute": [minutes[mi] for mi in minute_col],
        "symbol": [symbols[si] for si in symbol_col],
    }
    flat_panel = panel.reshape(n_min * n_sym, n_feat)
    for name, col in feature_idx.items():
        frame_data[name] = flat_panel[:, col]
    for label_name, arr in labels.items():
        frame_data[label_name] = arr.reshape(n_min * n_sym)

    frame = pl.DataFrame(frame_data).with_columns(
        pl.col("minute").cast(pl.Datetime("us", "UTC"))
    )
    return frame


def planted_signal_report() -> dict[str, object]:
    """What was planted -- so the harness can confirm the model recovered the RIGHT features."""
    return {
        "signal_features": SIGNAL_FEATURES,
        "labels": LABELS,
        "regime": "sin wave; flips beta sign of return_rank_15m, gates vwap_distance",
        "sector_features_used": SECTOR_FEATURES,
        "note": "all other ~510 features are pure noise; sector one-hots are truthful",
    }


def main() -> None:
    config = PanelConfig()
    config.feature_names = load_feature_names()
    frame = generate_panel(config)
    out = HERE / "synth_panel.parquet"
    frame.write_parquet(out)
    print(f"wrote {out}  shape={frame.shape}  ({config.n_symbols} symbols x {config.n_minutes} minutes)")
    print("planted:", json.dumps(planted_signal_report(), indent=2))


if __name__ == "__main__":
    main()
