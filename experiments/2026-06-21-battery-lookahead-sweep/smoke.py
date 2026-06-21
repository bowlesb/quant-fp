"""Smoke test: intraday look-ahead battery builds + grades on a few dates."""
from __future__ import annotations

from quantlib.battery.battery_config import (
    BatteryConfig,
    Cadence,
    DataSpec,
    LabelKind,
    SignalKind,
    StrategyConfig,
)
from quantlib.battery.battery_run import run_battery

GROUPS = {
    "price_returns": ["ret_5m", "ret_15m", "ret_30m", "ret_60m"],
    "volatility": ["realized_vol_5m", "realized_vol_30m"],
    "microstructure_burst": ["peak_trades_per_second_1m", "inter_arrival_cv_1m"],
    "trade_flow": ["signed_volume_15m", "trade_freq_15m"],
    "quote_spread": ["spread_bps_15m", "quote_imbalance_15m"],
}
DATA = DataSpec(
    cadence=Cadence.INTRADAY,
    date_start="2026-06-15",
    date_end="2026-06-18",
    universe_top=200,
    intraday_groups=GROUPS,
    intraday_horizons_min=(30,),
)
STRATEGIES = [
    StrategyConfig(name="ret15_upmove_h3", signal=SignalKind.FEATURE, signal_feature="ret_15m",
                   features=("ret_15m",), label=LabelKind.UP_MOVE_START, horizon=3, barrier_bps=50.0),
    StrategyConfig(name="composite_upmove_h3", signal=SignalKind.COMPOSITE,
                   label=LabelKind.UP_MOVE_START, horizon=3, barrier_bps=50.0),
    StrategyConfig(name="gbm_runup_h3", signal=SignalKind.GBM, label=LabelKind.FWD_MAX_RUNUP, horizon=3),
]
CONFIG = BatteryConfig(data=DATA, strategies=STRATEGIES, n_folds=3, min_train_rows=200, min_test_rows=30)

if __name__ == "__main__":
    print(run_battery(CONFIG).summary_md)
