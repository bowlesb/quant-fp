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

## Interpreting a PASS — it's an OPTIMISTic UPPER BOUND, not a deployable P&L

A battery `PASS` is the BEST case, not the live case. The backtest books a frictionless
~top/bottom-k cross-sectional basket over the (liquid-cut) panel — e.g. ~150 names per leg, every
short assumed borrowable, fills at the tradeable entry + the per-name half-spread. A LIVE container
on a shared paper/real Alpaca account trades a far smaller, harder book: typically a handful of
`easy_to_borrow` names per leg, real borrow availability/fees, partial fills, rejects, and
buying-power/PDT limits. So:

- A PASS says "this signal carried tradeable edge under idealized frictions" — it is the ceiling.
- It does NOT say "this nets X bps live." The gap (borrow, capacity, partials, the small live name
  count) is measured by the live `PaperExecutor`'s realized-slippage logging, not assumed away.
- The `by_stratum` liquidity breakdown + the `cost_curve` exist precisely so a PASS that lives only
  in the illiquid tail (the trap-#1 signature) is visible as NON-deployable despite the headline.

### The two concrete calibration gaps (BatteryAudit pass-2) — a PASS can OVERSTATE live edge

These are deliberate Phase-0 modeling choices, NOT bugs, but they mean the backtest book is more
diversified + more fillable than the live book, so the headline Sharpe is unachievable as-is:

1. **Full-fill assumption.** `long_short_per_name_cost` books a 100% fill on every selected name,
   charging only spread + borrow. The LIVE `build_basket` (services/executor) shorts ONLY
   `easy_to_borrow` names, EXCLUDES price < $5 and ETF-like symbols, and REFUSES baskets below
   `MIN_SCORE_SEP`. So the battery's short leg can include names the live container would never short
   — the backtest's short-side edge is an upper bound the borrow/price/ETF gate will trim.
2. **Breadth mismatch.** `frac=0.1` over `liquid_1500` books ~150 names/side; the live executor books
   `K_LONG=K_SHORT≈3` (~$100–200/name, ~$2–5k gross). The battery's per-period Sharpe comes from
   ~150-name diversification that the live 3–6-name book cannot reproduce — diversification Sharpe does
   NOT survive the breadth cut.

**Net:** a PASS demonstrates a signal EXISTS in a diversified, frictionless-fill basket;
live-achievability at 3–6 borrow-constrained names is a SEPARATE question, answered by the execution
layer + a borrow/price/breadth gate, not by the battery. (This is the AUC-insufficiency lesson, one
level up: predictive-in-the-backtest ≠ harvestable-live.)

Read the leaderboard as "what to promote to a live paper trial", never as a realized return.
