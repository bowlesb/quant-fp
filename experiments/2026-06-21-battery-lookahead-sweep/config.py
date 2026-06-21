"""LOOK-AHEAD-per-minute battery sweep — expressed AS a single BatteryConfig (#319 harness).

The net-new class the old archetype grid could NOT represent: per-minute look-ahead labels
(UP_MOVE_START triple-barrier first-touch + FWD_MAX_RUNUP forward extremum), graded over ONE
shared intraday microstructure feature matrix loaded once.

Run:
    python -m quantlib.battery.battery_cli --config <this file> --out <out_dir>

Each StrategyConfig is one line. The sweep =
    (trusted feature subset)
      x (signal: FEATURE single-probe / COMPOSITE / RIDGE / GBM)
      x (label: UP_MOVE_START {30,50}bps  /  FWD_MAX_RUNUP)
      x (horizon H in forward bars)
all over the SAME panel, with shuffle + predict-zero + per-name store-spread cost built in.

WINDOW: the recent 14-date common window (2026-05-29..06-18) at universe_top=200 — bounded so the
~10M-row intraday panel + the GBM/RIDGE fits stay memory- and time-tractable without starving live
capture, while still giving millions of gradable rows over many timestamps (ample power for the screen).
"""
from __future__ import annotations

from quantlib.battery.battery_config import (
    BatteryConfig,
    Cadence,
    DataSpec,
    LabelKind,
    SignalKind,
    StrategyConfig,
)

# Trusted intraday groups with deep, overlapping coverage in 2026-04-15..06-18 (43 common dates).
# A focused, non-redundant subset (NOT all 40 collinear return horizons) of plausible up-move
# ANTICIPATORS: short/mid momentum, realized vol, trade-burst intensity, signed flow, quote state.
GROUPS = {
    "price_returns": ["ret_5m", "ret_15m", "ret_30m", "ret_60m"],
    "volatility": ["realized_vol_5m", "realized_vol_30m"],
    "microstructure_burst": ["peak_trades_per_second_1m", "inter_arrival_cv_1m", "max_runup_1m"],
    "trade_flow": ["signed_volume_15m", "trade_freq_15m", "trade_rate_accel_1m"],
    "quote_spread": ["spread_bps_15m", "quote_imbalance_15m", "book_depth_1m"],
}
ALL_FEATURES = tuple(feat for feats in GROUPS.values() for feat in feats)

DATA = DataSpec(
    cadence=Cadence.INTRADAY,
    date_start="2026-05-29",
    date_end="2026-06-18",
    universe_top=200,
    intraday_groups=GROUPS,
    intraday_horizons_min=(30,),  # FORWARD_EXCESS horizon (unused by look-ahead labels; kept minimal)
)

# The look-ahead window is in FORWARD PANEL BARS. The intraday panel is at the native 1-minute bar
# cadence, so a horizon of H bars == H forward MINUTES. Sweep short windows where the per-minute
# look-ahead question ("is this the start of an up-move over the next H minutes?") is meaningful.
HORIZONS = (5, 15, 30)         # forward window in 1-min bars (== 5/15/30 minutes)
BARRIERS = (30.0, 50.0)        # +/- bps barriers for the triple-barrier UP_MOVE_START

# Single-feature PROBES: does ANY one anticipator rank the look-ahead outcome? (continuation sign).
# Reversion sign (-1) included for the return/flow features where a fade is plausible.
PROBE_FEATURES = [
    "ret_5m", "ret_15m", "ret_30m", "ret_60m",
    "realized_vol_5m", "realized_vol_30m",
    "peak_trades_per_second_1m", "inter_arrival_cv_1m", "max_runup_1m",
    "signed_volume_15m", "trade_freq_15m", "trade_rate_accel_1m",
    "spread_bps_15m", "quote_imbalance_15m", "book_depth_1m",
]
REVERSION_PROBE = ["ret_5m", "ret_15m", "ret_30m", "signed_volume_15m", "quote_imbalance_15m"]

PROBE_H = 15  # representative forward window (15 min) for the single-feature probes

_PROBES_UPMOVE = [
    StrategyConfig(
        name=f"probe_{feat}_up_h{PROBE_H}_b50",
        signal=SignalKind.FEATURE,
        signal_feature=feat,
        signal_sign=+1.0,
        features=(feat,),
        label=LabelKind.UP_MOVE_START,
        horizon=PROBE_H,
        barrier_bps=50.0,
    )
    for feat in PROBE_FEATURES
]
_PROBES_REV = [
    StrategyConfig(
        name=f"probe_{feat}_rev_up_h{PROBE_H}_b50",
        signal=SignalKind.FEATURE,
        signal_feature=feat,
        signal_sign=-1.0,
        features=(feat,),
        label=LabelKind.UP_MOVE_START,
        horizon=PROBE_H,
        barrier_bps=50.0,
    )
    for feat in REVERSION_PROBE
]
_PROBES_RUNUP = [
    StrategyConfig(
        name=f"probe_{feat}_runup_h{PROBE_H}",
        signal=SignalKind.FEATURE,
        signal_feature=feat,
        signal_sign=+1.0,
        features=(feat,),
        label=LabelKind.FWD_MAX_RUNUP,
        horizon=PROBE_H,
    )
    for feat in PROBE_FEATURES
]

# COMBINERS over the whole subset: COMPOSITE (no-fit EW z), RIDGE (linear), GBM (non-linear),
# across horizons + barriers + both look-ahead labels.
_COMPOSITE_UP = [
    StrategyConfig(
        name=f"composite_up_h{h}_b{int(b)}",
        signal=SignalKind.COMPOSITE,
        features=ALL_FEATURES,
        label=LabelKind.UP_MOVE_START,
        horizon=h,
        barrier_bps=b,
    )
    for h in HORIZONS
    for b in BARRIERS
]
# RIDGE / GBM are the only walk-forward FITS (expensive over the ~10M-row panel) — bound them to the
# barrier=50 column across the 3 horizons (the decisive non-linear-combiner cells), not the full grid.
_RIDGE_UP = [
    StrategyConfig(
        name=f"ridge_up_h{h}_b50",
        signal=SignalKind.RIDGE,
        features=ALL_FEATURES,
        label=LabelKind.UP_MOVE_START,
        horizon=h,
        barrier_bps=50.0,
    )
    for h in HORIZONS
]
_GBM_UP = [
    StrategyConfig(
        name=f"gbm_up_h{h}_b50",
        signal=SignalKind.GBM,
        features=ALL_FEATURES,
        label=LabelKind.UP_MOVE_START,
        horizon=h,
        barrier_bps=50.0,
    )
    for h in HORIZONS
]
_COMBINERS_RUNUP = [
    StrategyConfig(name=f"composite_runup_h{h}", signal=SignalKind.COMPOSITE,
                   features=ALL_FEATURES, label=LabelKind.FWD_MAX_RUNUP, horizon=h)
    for h in HORIZONS
] + [
    StrategyConfig(name=f"gbm_runup_h{PROBE_H}", signal=SignalKind.GBM,
                   features=ALL_FEATURES, label=LabelKind.FWD_MAX_RUNUP, horizon=PROBE_H),
    StrategyConfig(name=f"ridge_runup_h{PROBE_H}", signal=SignalKind.RIDGE,
                   features=ALL_FEATURES, label=LabelKind.FWD_MAX_RUNUP, horizon=PROBE_H),
]

STRATEGIES = (
    _PROBES_UPMOVE
    + _PROBES_REV
    + _PROBES_RUNUP
    + _COMPOSITE_UP
    + _RIDGE_UP
    + _GBM_UP
    + _COMBINERS_RUNUP
)

CONFIG = BatteryConfig(
    data=DATA,
    strategies=STRATEGIES,
    n_folds=5,
    min_train_rows=500,
    min_test_rows=50,
    seed=13,
)

if __name__ == "__main__":
    print(f"n_strategies={len(STRATEGIES)}")
