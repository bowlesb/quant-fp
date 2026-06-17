# W2 — Results: item-2.02 PEAD on LIQUID names

## Meta
- Panel: 7356 symbols, 378 trading days (2024-12-11 .. 2026-06-16).
- Earnings (item-2.02) events with bars: **7344**; in the **LIQUID tertile: 3838**.
- Liquid tier: 2447 symbols; top-100 megacap cut separately.
- Measured LIQUID half-spread: 6.70 bps -> round-trip cost 13.4 bps (1x), 26.8 bps (2x stress).
- Walk-forward split: OOS starts 2025-09-16 (last half of the window).
- Entry = D+1 open after available_at. Horizons (trading days): [1, 3, 5, 10, 20, 40].

## HEADLINE — LIQUID tertile PEAD drift (cohort minus same-date control, per-symbol demeaned, day-clustered)
Open-entry forward return, net of measured liquid round-trip cost. Per horizon 1d / 3d / 5d / 10d / 20d / 40d:

- FULL window (net): dm=-0.152% t=-0.66 n=3830 | dm=-0.025% t=-0.1 n=3822 | dm=+0.049% t=0.17 n=3811 | dm=+0.962% t=1.27 n=3773 | dm=-0.548% t=-0.8 n=3665 | dm=-0.719% t=-0.54 n=2115
- TRAIN (net):      dm=  n/a% t=None n=0 | dm=  n/a% t=None n=0 | dm=  n/a% t=None n=0 | dm=  n/a% t=None n=0 | dm=  n/a% t=None n=0 | dm=  n/a% t=None n=0
- **OOS (net):**     dm=-0.172% t=-0.74 n=3830 | dm=-0.088% t=-0.34 n=3822 | dm=-0.055% t=-0.18 n=3811 | dm=+0.804% t=1.06 n=3773 | dm=-0.752% t=-1.07 n=3665 | dm=-0.674% t=-0.55 n=2115
- Shuffle-canary OOS p95 (pct): ['+0.088', '+0.301', '+0.750', '+0.801', '+1.248', '+3.865']

## SIGNED L/S (the tradeable bet) — sign by D+1 reaction, drift measured from D+1 close, per-trade bootstrap CI (10k)
net% [boot CI lo, hi] n_trades, per horizon 1d / 3d / 5d / 10d / 20d / 40d:

- **LIQUID OOS net (1x cost):** -0.226% [-0.358,-0.095] n=3830 | -0.329% [-0.534,-0.119] n=3822 | -0.237% [-0.530,+0.039] n=3811 | -0.838% [-1.944,-0.061] n=3773 | -0.408% [-1.086,+0.208] n=3665 | +1.615% [-1.346,+6.372] n=2115
- LIQUID OOS net (2x cost):     -0.360% [-0.492,-0.229] n=3830 | -0.463% [-0.668,-0.253] n=3822 | -0.371% [-0.664,-0.095] n=3811 | -0.972% [-2.078,-0.195] n=3773 | -0.542% [-1.220,+0.074] n=3665 | +1.481% [-1.480,+6.238] n=2115
- LIQUID OOS gross:             -0.092% [-0.224,+0.039] n=3830 | -0.195% [-0.400,+0.015] n=3822 | -0.103% [-0.396,+0.173] n=3811 | -0.704% [-1.810,+0.073] n=3773 | -0.274% [-0.952,+0.342] n=3665 | +1.749% [-1.212,+6.506] n=2115
- LIQUID FULL net (1x cost):    -0.226% [-0.358,-0.095] n=3830 | -0.329% [-0.534,-0.119] n=3822 | -0.237% [-0.530,+0.039] n=3811 | -0.838% [-1.944,-0.061] n=3773 | -0.408% [-1.086,+0.208] n=3665 | +1.615% [-1.346,+6.372] n=2115

## Context cohorts (signed L/S OOS net, 1x cost)
- Top-100 megacap: -0.237% [-1.087,+0.636] n=150 | +0.263% [-1.218,+1.776] n=149 | -0.054% [-1.564,+1.521] n=149 | +0.076% [-2.206,+2.260] n=144 | +0.690% [-2.216,+3.543] n=133 | +2.408% [-3.734,+8.843] n=81
- Full universe (context): -0.566% [-1.297,-0.040] n=7256 | -0.982% [-2.528,+0.560] n=7236 | -0.951% [-2.524,+0.649] n=7207 | -1.494% [-3.146,+0.164] n=7135 | -1.266% [-4.668,+2.094] n=6868 | -5.114% [-13.473,+2.384] n=3986
- Mid tertile (context): +0.321% [-0.176,+1.099] n=2433 | -1.786% [-5.149,+0.505] n=2423 | -2.189% [-5.623,+0.222] n=2409 | -2.799% [-6.642,-0.213] n=2384 | -5.909% [-14.268,+0.408] n=2318 | -16.314% [-40.827,+3.412] n=1322
- Illiquid tertile (context): -4.314% [-9.408,-1.085] n=993 | -1.592% [-9.326,+8.136] n=991 | -0.364% [-8.251,+9.467] n=987 | -0.563% [-7.650,+9.079] n=978 | +7.706% [-6.639,+27.852] n=885 | -3.180% [-20.484,+16.356] n=549
