# Strategy Battery — Phase 0 (the cross-sectional edge-evaluation harness)

`evaluate_features(feature_set, date_range) -> BatteryReport` turns "I have a feature-set" into
"which strategy archetypes show a real, tradeable edge — with evidence", in ONE call, with the
anti-fooling discipline built IN (look-ahead, per-name cost, the two nulls, the data-trap guards,
multiple-comparisons correction). It WRAPS the proven discipline core (`quantlib/backtest.py` +
`research.py` + `labels.py`) — it does not reimplement it.

Full design: `docs/STRATEGY_BATTERY_DESIGN.md`.

## One-call usage

```python
from quantlib.battery import FeatureSetRef, LIQUID_1500, evaluate_features

# Daily-reduced trailing-EOD features (EOD / overnight / 2d / 3d horizons):
report = evaluate_features(
    FeatureSetRef(name="my-features", daily=True),
    ("2025-01-01", "2026-06-17"),
    universe=LIQUID_1500,
    daily_cache="/app/experiments/data/battery_daily_cache.parquet",  # reuse the stage-1 reduce
)
print(report.summary_md)     # the cell table + leaderboard, dashboard-renderable
report.leaderboard           # PASS cells surviving BY-FDR (empty = the honest, expected null)
```

```python
# Intraday store features (30m / 60m horizons), joined point-in-time from the store:
report = evaluate_features(
    FeatureSetRef(name="trusted-intraday", daily=False, groups={
        "price_returns": ["ret_1m", "ret_5m"], "volatility": ["realized_vol_10m"],
    }, horizons_min=(30, 60)),
    ("2026-05-15", "2026-06-17"),
)
```

## What a cell's `BacktestResult` carries (the un-foolable bundle)

- **net economics** net-of-REALISTIC per-name half-spread cost: `net_per_period`,
  `breakeven_cost_bps`, `sharpe_net`, `cost_curve` (net vs a cost-multiplier sweep — where the
  edge dies), `cost_used_bps` (the median per-name half-spread actually charged).
- **two nulls** `shuffle_canary` (within-timestamp label shuffle) + `predict_zero`; `edge_vs_shuffle`
  is the trust arbiter.
- **breakdowns** `by_stratum` (liquidity terciles — the illiquid-tail trap) + `by_regime`
  (up/down-market day).
- **significance** `nw_t` (Newey-West, overlap-aware).
- **directionality** `directional` (magnitude labels never graduate to a P&L verdict — Phase 1).
- **data-trap sanity** `SanityReport` ($1 floor / per-day winsor / label-std / entry >= 09:35 ET);
  a tripped guard FLAGS the verdict.
- **verdict** `PASS` / `FAIL` (the expected honest null) / `DESCRIPTIVE-ONLY` / `TRAP-FLAGGED`.

`BatteryReport.family_correction` is the **BY-FDR** across the whole battery (the multiple-comparisons
defense); the leaderboard reports only cells that PASS *and* survive correction.

## Performance

The panel is loaded ONCE per cadence (daily-reduced for EOD/overnight/multi-day; intraday-minute
for 30m/60m) and every cell evaluates over the resident column-major arrays — no per-cell store
re-read. `use_gbm=False` (default) is the fast path (the leading feature's rank, no model fit);
`use_gbm=True` is the opt-in deeper mode (a LightGBM per fold over the full feature-set — the config
that produced the published trusted-baseline / laneC numbers).

The daily stage-1 reduce (raw-bar glob) is cached to `daily_cache`; pass the same path to reuse it.

## Phase 1 seam (Rust path-dependent kernel)

The column-major `Panel` `(symbol_code, minute_epoch, <features>, high, low, close, volume,
half_spread)` sorted by `(symbol, minute)` is the SAME layout `rust/src/lib.rs` kernels consume,
so the Phase-1 `triple_barrier_first_touch` kernel + the triple-barrier / streak archetypes slot
into the existing `Strategy` protocol with zero conversion. The path-dependent fields on
`BacktestResult` (`up_vs_down_asymmetry`, `per_trade_pnl`) are stubbed for that phase.
