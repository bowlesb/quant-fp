"""The overnight-beta strategy container — the certified W11 edge, paper-only, as a MEASUREMENT instrument.

It trades the high-minus-low-beta L/S OVERNIGHT (enter at the close auction via Alpaca CLS, exit at the next
open auction via OPG), monthly beta-quintile rebalance, on the liquid universe excluding the speculation
cohort. Its PRIMARY job is to log model-expected vs REALIZED auction fills -> the real MOO/MOC slippage that
decides whether the certified +28-30 bps/day overnight net survives real fills (the one remaining unknown;
the backtest could only model 5 bps). Paper-only, smoke-style safety caps, own strat_overnightbeta schema.
"""
