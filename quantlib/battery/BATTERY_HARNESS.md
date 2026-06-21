# Strategy battery — the single configurable harness

`run_battery(config)` (CLI: `python -m quantlib.battery.battery_cli`) is the SINGLE entrypoint that runs
a **range of strategies over ONE feature matrix, loaded once, extremely quickly** — including strategies
whose label needs **per-minute look-ahead across all minutes of the dataset**.

It exists so a modeller tests an edge by **adding a config line**, never by hand-rolling a new experiment
script (load panel + winsorize + IC + NW + cost + shuffle, re-rolled every time — the pattern every
recent experiment repeated).

## The one call

```python
from quantlib.battery.battery_run import run_battery
from quantlib.battery.battery_config import (
    BatteryConfig, DataSpec, StrategyConfig, Cadence, SignalKind, LabelKind,
)

config = BatteryConfig(
    data=DataSpec(cadence=Cadence.DAILY, date_start="2025-12-01", date_end="2026-06-17",
                  universe_top=500, daily_cache="experiments/data/battery_daily_cache.parquet"),
    strategies=[
        StrategyConfig(name="mom_ret20", signal=SignalKind.FEATURE, signal_feature="ret_20d",
                       features=("ret_20d",), label=LabelKind.FORWARD_EXCESS, horizon=1),
        StrategyConfig(name="rev_ret1",  signal=SignalKind.FEATURE, signal_feature="ret_1d",
                       signal_sign=-1.0, features=("ret_1d",), horizon=1),
        StrategyConfig(name="gbm_all",   signal=SignalKind.GBM, horizon=1),  # features=None -> whole matrix
        StrategyConfig(name="upmove",    signal=SignalKind.COMPOSITE,        # per-minute LOOK-AHEAD
                       label=LabelKind.UP_MOVE_START, horizon=3, barrier_bps=50.0),
    ],
)
report = run_battery(config)
print(report.summary_md)   # cell table + leaderboard + wall-time
report.leaderboard         # PASS strategies (empty = the honest, expected null)
```

CLI form (a config file exposing `CONFIG: BatteryConfig` — see `configs/demo.py`):

```bash
python -m quantlib.battery.battery_cli --config quantlib/battery/configs/demo.py --out /tmp/battery
```

## Adding a strategy = adding ONE `StrategyConfig`

A `StrategyConfig` is the full configurable point in the strategy space:

| field | what it declares |
|---|---|
| `features` | the feature subset (tuple of panel column names); `None` = the whole matrix |
| `signal` | `FEATURE` (rank one signed feature), `COMPOSITE` (EW z-composite), `RIDGE`, `GBM` |
| `signal_feature` / `signal_sign` | which feature to rank, and `+1`/`-1` (continuation vs reversion) — `FEATURE` only |
| `label` | `FORWARD_EXCESS`, or a per-minute look-ahead: `UP_MOVE_START`, `FWD_MAX_RUNUP` |
| `horizon` | forward horizon: trading DAYS (daily cadence) / MINUTES (intraday cadence) |
| `barrier_bps` | the +/- barrier for the `UP_MOVE_START` triple-barrier label |
| `frac` | top/bottom fraction for the EW dollar-neutral L/S basket |

The shared knobs (cost, slippage, borrow, n_folds, baselines) live ONCE on the `BatteryConfig`, so every
strategy is booked and graded under the same realistic model.

## Per-minute look-ahead (the new capability)

`LabelKind.UP_MOVE_START` asks, at EACH entry row: *is this the start of an up-move over the next H bars?*
— a triple-barrier first-touch label (+1 if the forward path hits `+barrier_bps` before `-barrier_bps`
within H bars, -1 if down first, 0 on timeout). `LabelKind.FWD_MAX_RUNUP` is the forward-window extremum
(max forward run-up). Both are **vectorized per contiguous symbol block** (the panel is sorted by
`(symbol_code, minute)`, so a forward window never crosses a symbol), with a small numpy loop over the H
forward offsets — NOT a Python loop over the millions of dataset rows. Measured: the up-move label over
56,814 rows in ~12ms.

> Rust seam (per the established Rust=path-dependent-kernels-only policy): the per-block forward-offset
> loop is the one path-dependent kernel here. At the current panel sizes the numpy version is already
> ~12ms, so Rust is **not** warranted yet; if the look-ahead labels are run over the full deep panel
> (millions of intraday rows x many barriers), `lookahead.py`'s `_block_bounds` + the offset loop is the
> isolated kernel to port (it consumes the same column-major `(symbol_code, entry, high, low)` arrays the
> existing `rust/src/lib.rs` kernels assume).

## Why it is fast

The panel (the feature matrix) is loaded ONCE and shared by reference across every strategy — no
per-strategy rebuild. Each strategy's cost is only its own fit + walk-forward apply + grading over the
resident arrays. The report prints `panel_load` vs `eval` vs `total` and `s/strategy`, so "fast" is a
measured number. Demonstrated: 8 strategies (momentum + reversal + GBM + 2 look-ahead) over a
56,814-row x 13-feature x 495-symbol daily matrix in ~31s total, panel loaded in 0.12s; 20 cheap
single-feature strategies in ~8s (~0.4s each).

## Anti-fooling (built in)

Every strategy carries, in ONE run: the **shuffle null** (within-timestamp label shuffle — the
leakage/overfit canary) and the **predict-zero null** ($0 book), plus the per-name half-spread cost,
Newey-West t, and the tradeable-entry / $1-floor discipline baked into the panel build. A `PASS` verdict
requires net-positive **and** beating its own shuffle **and** `|NW t| >= 2` — deliberately conservative.
A `PASS` is "promote to a live paper trial", never a realized-return claim.

## Relationship to the older entrypoints

- `quantlib.harness.run_strategy(HarnessConfig)` runs ONE strategy and reloads the panel each call — fine
  for a single config, but running N strategies meant N panel builds + a hand-rolled loop. `run_battery`
  reuses its proven panel/model/cost machinery but loads the panel once for the whole LIST.
- `quantlib.battery.evaluate_features(FeatureSetRef)` runs a FROZEN archetype grid (cross-sectional L/S
  over a fixed `Horizon` enum) — it can't express a single signed-feature decile signal, a custom horizon,
  or a per-minute look-ahead label, which is why modellers hand-rolled instead. `run_battery` is the
  configurable replacement: any mix of signals/labels/horizons as a declared list.
