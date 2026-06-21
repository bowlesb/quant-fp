"""Demo battery config — proves the harness on REAL data: a momentum family + a reversal family + a
per-minute LOOK-AHEAD strategy, all over ONE shared daily feature matrix.

Run:
    python -m quantlib.battery.battery_cli --config quantlib/battery/configs/demo.py --out /tmp/battery

Each entry below is a `StrategyConfig` — ONE config line per strategy. Adding a strategy is adding one
entry to `STRATEGIES`, never a new experiment script. The whole list runs over the SAME panel loaded once.
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

DATA = DataSpec(
    cadence=Cadence.DAILY,
    date_start="2025-12-01",
    date_end="2026-06-17",
    universe_top=500,
    daily_cache="experiments/data/battery_daily_cache.parquet",
)

# MOMENTUM family: rank a trailing-return feature, higher -> LONG (continuation), several horizons.
_MOMENTUM = [
    StrategyConfig(
        name=f"mom_{feat}_h{horizon}",
        signal=SignalKind.FEATURE,
        signal_feature=feat,
        signal_sign=+1.0,
        features=(feat,),
        label=LabelKind.FORWARD_EXCESS,
        horizon=horizon,
    )
    for feat, horizon in [("ret_5d", 1), ("ret_10d", 1), ("ret_20d", 2)]
]

# REVERSAL family: rank the SAME trailing-return feature with inverted sign (recent winners -> SHORT).
_REVERSAL = [
    StrategyConfig(
        name=f"rev_{feat}_h{horizon}",
        signal=SignalKind.FEATURE,
        signal_feature=feat,
        signal_sign=-1.0,
        features=(feat,),
        label=LabelKind.FORWARD_EXCESS,
        horizon=horizon,
    )
    for feat, horizon in [("ret_1d", 1), ("ret_5d", 1)]
]

# COMBINER: a GBM over the whole trailing-feature set, forward-1d excess (the deeper non-linear screen).
_COMBINER = [
    StrategyConfig(
        name="gbm_all_h1",
        signal=SignalKind.GBM,
        features=None,  # whole matrix
        label=LabelKind.FORWARD_EXCESS,
        horizon=1,
    )
]

# LOOK-AHEAD-PER-MINUTE: grade the composite signal against a per-row triple-barrier "is this the start
# of an up-move over the next H bars?" label (+50bps before -50bps within H), vectorized across all rows.
_LOOKAHEAD = [
    StrategyConfig(
        name="composite_upmove_h3",
        signal=SignalKind.COMPOSITE,
        features=None,
        label=LabelKind.UP_MOVE_START,
        horizon=3,
        barrier_bps=50.0,
    ),
    StrategyConfig(
        name="mom20_runup_h3",
        signal=SignalKind.FEATURE,
        signal_feature="ret_20d",
        features=("ret_20d",),
        label=LabelKind.FWD_MAX_RUNUP,
        horizon=3,
    ),
]

STRATEGIES = _MOMENTUM + _REVERSAL + _COMBINER + _LOOKAHEAD

CONFIG = BatteryConfig(data=DATA, strategies=STRATEGIES)
