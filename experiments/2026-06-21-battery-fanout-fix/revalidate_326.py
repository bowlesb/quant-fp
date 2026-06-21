"""Re-validate the #326 look-ahead directional-null verdict on the FAN-OUT-CORRECTED panel.

#326's panel was inflated by the cross-shard join fan-out (3.08M rows). With the fix, the same window
yields a CLEAN panel (~13 rows/sym/day). We re-run a representative subset of #326's directional cells
(single-feature UP_MOVE_START probes + COMPOSITE + GBM) and report the corrected ICs. The numbers should
be SMALLER (the inflation duplicated rows, biasing per-timestamp IC), but the directional-null verdict
(no up_move_start strategy clears net-positive + beats-shuffle + |NW t|>=2) must SURVIVE quantitatively.
"""
from __future__ import annotations

from quantlib.battery.battery_config import (
    BatteryConfig, Cadence, DataSpec, LabelKind, SignalKind, StrategyConfig,
)
from quantlib.battery.battery_run import run_battery

GROUPS = {
    "price_returns": ["ret_5m", "ret_15m", "ret_30m", "ret_60m"],
    "volatility": ["realized_vol_5m", "realized_vol_30m"],
    "microstructure_burst": ["peak_trades_per_second_1m", "inter_arrival_cv_1m", "max_runup_1m"],
    "trade_flow": ["signed_volume_15m", "trade_freq_15m", "trade_rate_accel_1m"],
    "quote_spread": ["spread_bps_15m", "quote_imbalance_15m", "book_depth_1m"],
}
ALL = tuple(f for fs in GROUPS.values() for f in fs)
DATA = DataSpec(cadence=Cadence.INTRADAY, date_start="2026-05-29", date_end="2026-06-18",
                universe_top=200, intraday_groups=GROUPS, intraday_horizons_min=(30,))

# the directional cells most load-bearing for #326's verdict (the best directional probes + combiners).
PROBES = ["ret_15m", "quote_imbalance_15m", "realized_vol_30m", "spread_bps_15m", "max_runup_1m"]
STRATEGIES = (
    [StrategyConfig(name=f"probe_{f}_up_h15_b50", signal=SignalKind.FEATURE, signal_feature=f,
                    features=(f,), label=LabelKind.UP_MOVE_START, horizon=15, barrier_bps=50.0)
     for f in PROBES]
    + [StrategyConfig(name=f"probe_{f}_runup_h15", signal=SignalKind.FEATURE, signal_feature=f,
                      features=(f,), label=LabelKind.FWD_MAX_RUNUP, horizon=15)
       for f in ["realized_vol_30m", "spread_bps_15m"]]
    + [StrategyConfig(name="composite_up_h15_b50", signal=SignalKind.COMPOSITE, features=ALL,
                      label=LabelKind.UP_MOVE_START, horizon=15, barrier_bps=50.0),
       StrategyConfig(name="gbm_up_h15_b50", signal=SignalKind.GBM, features=ALL,
                      label=LabelKind.UP_MOVE_START, horizon=15, barrier_bps=50.0)]
)
CONFIG = BatteryConfig(data=DATA, strategies=STRATEGIES, n_folds=5, min_train_rows=500, min_test_rows=50)

if __name__ == "__main__":
    rep = run_battery(CONFIG)
    print(rep.summary_md)
