# Battery LOOK-AHEAD-per-minute sweep (2026-06-21)

A disciplined edge sweep of the **look-ahead-per-minute** strategy class, expressed AS a single
`BatteryConfig` over the merged `#319` battery harness — the intended modeller workflow (one config
run → trustworthy verdict), NOT a hand-rolled experiment script.

## Why this is net-new (not a re-run of the settled nulls)

The settled nulls (cross-sectional direction / order-flow / overnight / weekly-reversal / portfolio-
combo) all tested **fixed-horizon forward returns** or **cross-sectional direction**. This sweep tests
the battery's **per-minute look-ahead labels**, which the old frozen archetype grid literally could not
represent:

- `UP_MOVE_START` — triple-barrier first-touch: *at THIS minute, does the forward path hit `+barrier`
  before `-barrier` within the next H bars?* (`+1` up-first / `-1` down-first / `0` timeout).
- `FWD_MAX_RUNUP` — forward-window extremum: the max forward run-up over the next H bars.

Both are computed point-in-time per entry row but read FORWARD bars — they are LABELS, graded against,
never features. Vectorized per contiguous symbol block (no Python per-row loop).

## The config (the whole experiment = one declarative file)

`config.py` → `CONFIG: BatteryConfig`. Run:

```bash
docker run --rm -v <worktree>:/app -v fp_store_real:/store:ro \
  -e STORE_ROOT=/store -e PYTHONPATH=/app -e USE_REALIZED_COST=0 -w /app \
  --memory=48g --cpus=6 fp-dev \
  python3 -m quantlib.battery.battery_cli \
    --config experiments/2026-06-21-battery-lookahead-sweep/config.py \
    --out experiments/2026-06-21-battery-lookahead-sweep/out
```

- **Code SHA**: `320563b` (origin/main; battery harness `#319`). No fingerprint/feature/registry change
  — the battery READS the store matrix.
- **Cadence**: INTRADAY (the look-ahead labels are genuinely per-minute). Panel = the native 1-min bar
  grid, so a horizon of H bars == H forward MINUTES.
- **Window**: 2026-05-29..06-18 (14 recent common dates across the deep groups), `universe_top=200`.
  Bounded so the ~10M-row intraday panel + the GBM/RIDGE fits stay memory/time-tractable without
  starving live capture; still millions of gradable rows over many timestamps (ample power).
- **Feature matrix** (trusted intraday groups, focused non-redundant subset of plausible up-move
  ANTICIPATORS — NOT all 40 collinear return horizons):
  - `price_returns`: ret_5m/15m/30m/60m
  - `volatility`: realized_vol_5m/30m
  - `microstructure_burst`: peak_trades_per_second_1m, inter_arrival_cv_1m, max_runup_1m
  - `trade_flow`: signed_volume_15m, trade_freq_15m, trade_rate_accel_1m
  - `quote_spread`: spread_bps_15m, quote_imbalance_15m, book_depth_1m
- **52 strategies**: 35 single-feature FEATURE probes (continuation + a reversion set) × {UP_MOVE_START,
  FWD_MAX_RUNUP}; 9 COMPOSITE; 4 RIDGE + 4 GBM combiners over the whole subset. Horizons H ∈ {5,15,30}
  min; barriers {30,50} bps. Combiner FITS bounded to the barrier=50 column (the expensive cells).
- **Cost**: `USE_REALIZED_COST=0` → the store `quote_spread` per-name half-spread (real measured
  spread/2; far more realistic than the flat 3bps stub the G0 work flagged as 2.8x-undercharged). The
  per-minute tape-measured realized-cost path was prohibitively slow over 14 dates.
- **Anti-fooling (built in, per strategy, one run)**: within-timestamp label SHUFFLE null + predict-zero
  null + per-name half-spread cost + Newey-West t. `PASS` = net-positive AND beats own shuffle AND
  `|NW t| >= 2` (deliberately conservative).

## IMPORTANT grading caveat (surfaced honestly)

For the look-ahead labels, the battery books the L/S economics with `realized = label`. So:
- For `UP_MOVE_START` (label ∈ {-1,0,+1}) the "net_per_period" is a directional **hit-spread** of the
  long-minus-short basket on the barrier outcome, net of the per-name spread cost charged in
  barrier-units — a meaningful *separation* metric, but NOT a dollar return.
- For `FWD_MAX_RUNUP` (a positive-only magnitude ~bps) the "net/period", "breakeven_bps", "sharpe_net"
  are NOT real-money quantities (a positive-only label inflates gross). For run-up strategies, read
  **IC and edge_vs_shuffle ONLY**; ignore the $ columns.

So the trustworthy verdict signal is: **edge_vs_shuffle > 0 AND |NW t| >= 2**, with the UP_MOVE_START
net/breakeven as a secondary cost-realism check. A real candidate would clear all of those AND survive
the realistic cost; a lone outlier cell warrants suspicion, not celebration.

## Results

See `RESULTS.md` (leaderboard + full cell table + wall-time + verdict) and `out/report.md` /
`out/report.json`.
