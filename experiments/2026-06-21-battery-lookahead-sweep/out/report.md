# Battery report

cadence=intraday  range=2026-05-29..2026-06-18  universe_top=200  panel=3085192rows x 15feat x 200sym  strategies=52
panel_load=24.071s  eval=1219.594s  **total=1243.664s**  (**23.454s/strategy** over the shared matrix)

| strategy | signal | label | H | n_rows | IC | shuffle_IC | edge | NW t | net/period | breakeven_bps | cost_bps | sharpe_net | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| probe_ret_5m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | 0.0018 | 0.0029 | -0.0011 | 0.09 | 0.0447 | 150.39 | 1.18 | 4.71 | FAIL |
| probe_ret_15m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | 0.0292 | -0.0021 | 0.0312 | 1.38 | 0.0297 | 102.87 | 1.18 | 3.14 | FAIL |
| probe_ret_30m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | 0.0058 | 0.0025 | 0.0033 | 0.28 | -0.0151 | -41.81 | 1.18 | -1.52 | FAIL |
| probe_ret_60m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | 0.0038 | 0.0022 | 0.0016 | 0.17 | -0.0471 | -197.83 | 1.18 | -4.71 | FAIL |
| probe_realized_vol_5m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0245 | 0.0096 | -0.0341 | -0.90 | -0.0376 | -179.66 | 1.18 | -3.37 | FAIL |
| probe_realized_vol_30m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0406 | -0.0005 | -0.0401 | -1.33 | -0.0665 | -515.37 | 1.18 | -5.77 | FAIL |
| probe_peak_trades_per_second_1m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0039 | -0.0074 | 0.0034 | -0.28 | -0.0373 | -141.36 | 1.18 | -4.73 | FAIL |
| probe_inter_arrival_cv_1m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0025 | -0.0014 | -0.0012 | -0.24 | -0.0270 | -93.10 | 1.18 | -3.84 | FAIL |
| probe_max_runup_1m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0051 | -0.0064 | 0.0013 | -0.28 | 0.0061 | 50.87 | 1.18 | 0.66 | FAIL |
| probe_signed_volume_15m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0043 | 0.0037 | -0.0080 | -0.43 | 0.0250 | 103.76 | 1.18 | 3.82 | FAIL |
| probe_trade_freq_15m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0014 | -0.0091 | 0.0077 | -0.08 | 0.0058 | 78.87 | 1.18 | 0.65 | FAIL |
| probe_trade_rate_accel_1m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | 0.0012 | -0.0102 | 0.0114 | 0.12 | 0.0177 | 61.51 | 1.18 | 2.52 | FAIL |
| probe_spread_bps_15m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0292 | 0.0040 | -0.0333 | -1.48 | -0.0172 | -219.20 | 1.18 | -1.93 | FAIL |
| probe_quote_imbalance_15m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | 0.0177 | 0.0086 | 0.0091 | 1.93 | 0.1116 | 515.40 | 1.18 | 18.06 | FAIL |
| probe_book_depth_1m_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0118 | 0.0008 | -0.0125 | -1.00 | -0.0243 | -224.38 | 1.18 | -3.48 | FAIL |
| probe_ret_5m_rev_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0018 | -0.0029 | 0.0011 | -0.09 | -0.0479 | -147.77 | 1.18 | -5.04 | FAIL |
| probe_ret_15m_rev_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0292 | 0.0021 | -0.0312 | -1.38 | -0.0336 | -102.87 | 1.18 | -3.55 | FAIL |
| probe_ret_30m_rev_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0058 | -0.0025 | -0.0033 | -0.28 | 0.0112 | 41.81 | 1.18 | 1.13 | FAIL |
| probe_signed_volume_15m_rev_up_h15_b50 | feature | up_move_start | 15 | 3080089 | 0.0043 | -0.0037 | 0.0080 | 0.43 | -0.0270 | -103.76 | 1.18 | -4.13 | FAIL |
| probe_quote_imbalance_15m_rev_up_h15_b50 | feature | up_move_start | 15 | 3080089 | -0.0177 | -0.0086 | -0.0091 | -1.93 | -0.1146 | -515.40 | 1.18 | -18.56 | FAIL |
| probe_ret_5m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.0220 | 0.0018 | 0.0203 | 0.58 | -0.0086 | -21.26 | 1.18 | -0.52 | FAIL |
| probe_ret_15m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.0153 | 0.0157 | -0.0004 | 0.43 | -0.0971 | -309.41 | 1.18 | -5.83 | FAIL |
| probe_ret_30m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.0091 | 0.0120 | -0.0028 | 0.23 | -0.0670 | -207.27 | 1.18 | -4.01 | FAIL |
| probe_ret_60m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.0281 | 0.0117 | 0.0164 | 0.68 | 0.0666 | 295.91 | 1.18 | 3.98 | FAIL |
| probe_realized_vol_5m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.3094 | 0.0110 | 0.2984 | 7.63 | 0.2210 | 1098.22 | 1.18 | 13.53 | PASS |
| probe_realized_vol_30m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.3346 | 0.0058 | 0.3289 | 7.84 | 0.2231 | 1760.89 | 1.18 | 13.66 | PASS |
| probe_peak_trades_per_second_1m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.0662 | -0.0010 | 0.0672 | 2.22 | 0.1304 | 521.28 | 1.18 | 7.86 | PASS |
| probe_inter_arrival_cv_1m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | -0.0033 | -0.0133 | 0.0100 | -0.11 | -0.0382 | -134.62 | 1.18 | -2.29 | FAIL |
| probe_max_runup_1m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.1544 | 0.0142 | 0.1402 | 4.25 | 0.1161 | 873.32 | 1.18 | 6.98 | PASS |
| probe_signed_volume_15m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | -0.0039 | 0.0109 | -0.0148 | -0.12 | -0.0011 | -0.43 | 1.18 | -0.07 | FAIL |
| probe_trade_freq_15m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.0618 | -0.0059 | 0.0678 | 1.65 | 0.0193 | 247.58 | 1.18 | 1.15 | FAIL |
| probe_trade_rate_accel_1m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | -0.0209 | -0.0041 | -0.0168 | -0.74 | -0.0561 | -176.23 | 1.18 | -3.36 | FAIL |
| probe_spread_bps_15m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.2358 | 0.0157 | 0.2201 | 7.17 | 0.2284 | 3054.41 | 1.18 | 13.65 | PASS |
| probe_quote_imbalance_15m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.0038 | -0.0097 | 0.0135 | 0.13 | 0.0349 | 165.88 | 1.18 | 2.03 | FAIL |
| probe_book_depth_1m_runup_h15 | feature | fwd_max_runup | 15 | 3080089 | 0.0014 | -0.0114 | 0.0129 | 0.05 | 0.0221 | 215.15 | 1.18 | 1.29 | FAIL |
| composite_up_h5_b30 | composite | up_move_start | 5 | 3080089 | 0.0099 | -0.0041 | 0.0140 | 0.62 | 0.0539 | 211.08 | 1.18 | 4.82 | FAIL |
| composite_up_h5_b50 | composite | up_move_start | 5 | 3080089 | 0.0098 | -0.0061 | 0.0159 | 0.57 | 0.0227 | 92.51 | 1.18 | 2.55 | FAIL |
| composite_up_h15_b30 | composite | up_move_start | 15 | 3080089 | 0.0088 | 0.0023 | 0.0065 | 0.48 | 0.1076 | 411.66 | 1.18 | 4.60 | FAIL |
| composite_up_h15_b50 | composite | up_move_start | 15 | 3080089 | 0.0064 | -0.0037 | 0.0101 | 0.35 | 0.0593 | 229.75 | 1.18 | 7.05 | FAIL |
| composite_up_h30_b30 | composite | up_move_start | 30 | 3080089 | 0.0012 | -0.0002 | 0.0014 | 0.06 | 0.0009 | 8.76 | 1.18 | 0.08 | FAIL |
| composite_up_h30_b50 | composite | up_move_start | 30 | 3080089 | 0.0074 | -0.0064 | 0.0138 | 0.39 | 0.0362 | 144.04 | 1.18 | 4.16 | FAIL |
| ridge_up_h5_b50 | ridge | up_move_start | 5 | 3080089 | -0.0033 | -0.0019 | -0.0014 | -0.20 | -0.0245 | -82.84 | 1.18 | -3.00 | FAIL |
| ridge_up_h15_b50 | ridge | up_move_start | 15 | 3080089 | -0.0043 | 0.0004 | -0.0047 | -0.26 | -0.0200 | -74.70 | 1.18 | -2.52 | FAIL |
| ridge_up_h30_b50 | ridge | up_move_start | 30 | 3080089 | 0.0017 | -0.0035 | 0.0052 | 0.09 | -0.0395 | -162.59 | 1.18 | -4.89 | FAIL |
| gbm_up_h5_b50 | gbm | up_move_start | 5 | 3080089 | -0.0187 | 0.0036 | -0.0223 | -1.69 | -0.0402 | -127.07 | 1.18 | -5.50 | FAIL |
| gbm_up_h15_b50 | gbm | up_move_start | 15 | 3080089 | -0.0186 | 0.0043 | -0.0229 | -1.59 | -0.0262 | -90.70 | 1.18 | -3.60 | FAIL |
| gbm_up_h30_b50 | gbm | up_move_start | 30 | 3080089 | -0.0153 | -0.0028 | -0.0124 | -1.31 | 0.0233 | 95.25 | 1.18 | 2.99 | FAIL |
| composite_runup_h5 | composite | fwd_max_runup | 5 | 3080089 | 0.1115 | -0.0042 | 0.1157 | 3.31 | 0.0688 | 267.40 | 1.18 | 4.11 | PASS |
| composite_runup_h15 | composite | fwd_max_runup | 15 | 3080089 | 0.1291 | 0.0061 | 0.1230 | 3.73 | 0.0636 | 245.71 | 1.18 | 3.80 | PASS |
| composite_runup_h30 | composite | fwd_max_runup | 30 | 3080089 | 0.1035 | 0.0033 | 0.1002 | 2.66 | 0.0207 | 84.70 | 1.18 | 1.24 | PASS |
| gbm_runup_h15 | gbm | fwd_max_runup | 15 | 3080089 | 0.2488 | 0.0034 | 0.2454 | 6.75 | 0.0458 | 219.54 | 1.18 | 2.73 | PASS |
| ridge_runup_h15 | ridge | fwd_max_runup | 15 | 3080089 | 0.2366 | 0.0003 | 0.2363 | 5.45 | 0.0295 | 180.77 | 1.18 | 1.76 | PASS |

## Leaderboard (PASS = net-positive + beats shuffle + |NW t|>=2)

1. **probe_realized_vol_30m_runup_h15** — sharpe_net=13.66 IC=0.3346 NW t=7.84 breakeven=1760.89bps
2. **probe_spread_bps_15m_runup_h15** — sharpe_net=13.65 IC=0.2358 NW t=7.17 breakeven=3054.41bps
3. **probe_realized_vol_5m_runup_h15** — sharpe_net=13.53 IC=0.3094 NW t=7.63 breakeven=1098.22bps
4. **probe_peak_trades_per_second_1m_runup_h15** — sharpe_net=7.86 IC=0.0662 NW t=2.22 breakeven=521.28bps
5. **probe_max_runup_1m_runup_h15** — sharpe_net=6.98 IC=0.1544 NW t=4.25 breakeven=873.32bps
6. **composite_runup_h5** — sharpe_net=4.11 IC=0.1115 NW t=3.31 breakeven=267.40bps
7. **composite_runup_h15** — sharpe_net=3.80 IC=0.1291 NW t=3.73 breakeven=245.71bps
8. **gbm_runup_h15** — sharpe_net=2.73 IC=0.2488 NW t=6.75 breakeven=219.54bps
9. **ridge_runup_h15** — sharpe_net=1.76 IC=0.2366 NW t=5.45 breakeven=180.77bps
10. **composite_runup_h30** — sharpe_net=1.24 IC=0.1035 NW t=2.66 breakeven=84.70bps
