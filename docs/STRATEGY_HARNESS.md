# Strategy Harness — train → apply → evaluate, fast, production-portable

`run_strategy(config) -> StrategyReport` (+ a CLI) turns a `(tickers × features × time)` panel into
"here is the money a long/short-by-percentile strategy made, the percentile-threshold diagnostic curve,
and the shuffle / predict-zero baselines" — in ONE configurable, fast call. It is the model-driven layer
on top of the Phase-0 `quantlib/battery/` (which is single-feature-rank, null-hunting + BY-FDR); the
harness adds offline model TRAINING (walk-forward) + the conservative percentile-threshold diagnostics +
the organized $ P&L output Ben asked for.

This builds directly on `STRATEGY_BATTERY_PORTABILITY.md` — the same shared-decision-core invariant —
and reuses its `quantlib/strategy_core/` (`CrossSectionalLS.decide`, the `PanelCrossSection` /
`BusCrossSection` adapters, the per-name cost model) without re-implementing anything.

## "Just run it"

```python
from quantlib.harness import HarnessConfig, run_strategy

report = run_strategy(HarnessConfig(daily_cache="experiments/data/battery_daily_cache.parquet"))
print(report.summary_md)        # the money + the percentile-threshold curve + the baselines
report.money.equity_curve       # ($-on-capital) per period
report.threshold_curve.cuts     # the per-percentile-cut diagnostics
```

```bash
python -m quantlib.harness \
  --daily-cache experiments/data/battery_daily_cache.parquet \
  --model gbm --label-horizon-days 1 --frac 0.10 --capital 1000000 \
  --universe-top 500 --out /tmp/harness_demo
# writes report.md / equity_curve.csv / threshold_curve.csv / report.json
```

## The pipeline

1. **load panel** — reuse the battery `build_daily_panel` (cached raw-bar reduce) or `build_intraday_panel`
   (trusted point-in-time store features). Same column-major `Panel`, same $1 floor / liquidity floor /
   tradeable-entry (≥09:35 ET) discipline.
2. **label** — per-row forward CROSS-SECTIONAL EXCESS return at the configured horizon (`quantlib/harness/
   labels.py`), derived off the panel's resident arrays (gap-safe per-symbol forward shift). Excess so a
   market move doesn't masquerade as signal. NaN at each symbol block's tail (no forward → no look-ahead).
3. **walk-forward** — expanding folds (`quantlib.backtest.walk_forward_folds`), each PURGED by the label
   horizon so no training label peeks into its test block.
4. **train (offline)** — fit a `RankModel` on each train fold: GBM (LightGBM, robust default), RIDGE
   (closed-form numpy), or COMPOSITE (no-fit z-score screen). The trained model is FROZEN into an artifact.
5. **apply (the shared decide)** — score the test fold via `CrossSectionalLS(model=frozen).score(cs)` — the
   EXACT method a live container's `decide` calls. Applied vectorized here (predict the whole fold matrix at
   once, rank top/bottom-`frac` per timestamp); per-single-vector live.
6. **book $ P&L** — the configured top/bottom-`frac` L/S basket through the SHARED per-name half-spread cost
   model (`long_short_per_name_cost`), rolled into the $ equity curve on the book capital.
7. **diagnostics + baselines** — the percentile-threshold curve, AUC/IC, and the shuffle / predict-zero nulls.

## ⭐ The PRODUCTION-PORTABLE `decide()` invariant (the make-or-break)

The model-application logic is `quantlib.strategy_core.cross_sectional_ls.CrossSectionalLS` — written ONCE,
applied two ways:

- **(a) VECTORIZED over the panel** (the harness fast batch backtest): the frozen model predicts the whole
  test fold's feature matrix at once, then the per-timestamp top/bottom-k is columnar.
- **(b) PER single vector live** (a container's `decide()` over a bus `FeatureView`): the SAME
  `core.score(cross_section)` on one cross-section per cycle.

The TRAINING is offline (walk-forward on the train fold); the trained model is a frozen `RankModel`
artifact the SAME `decide` loads — exactly the feature platform's pattern (backtest-only fold
orchestration, one shared scoring path). A harness-validated strategy drops into a live container via the
existing `production_execution.py` + `BusCrossSection` path with ZERO re-implementation of the decision:
graduation is **configuration** (the frozen model + the L/S frac), not code.

**Proven by `tests/harness/test_harness_portability.py`:**
- `test_decide_parity_panel_vs_bus_{gbm,ridge}` — the SAME frozen-model `decide` over a `PanelCrossSection`
  and a `BusCrossSection` built from IDENTICAL data yields IDENTICAL target books (execution-agnostic).
- `test_batch_vs_per_event_select_identical` — the harness's vectorized batch apply selects the SAME legs as
  per-event `decide`, timestamp by timestamp (speed-agnostic — no backtest-only fast path can drift).
- `test_frozen_model_is_the_shared_object` — the frozen model scores a live-shaped bus cross-section bit-for-
  bit identically to the panel scoring.

## ⭐ The PERCENTILE-THRESHOLD CURVE (the headline diagnostic)

Ben's emphasis — "more diagnostic than AUC, conservative thresholds of a percentile". For each cut in
{1, 2, 5, 10, 20, 33, 50}% the harness books the top/bottom-cut% L/S basket and reports, net of per-name
cost: directional **precision** (P(score predicts correct sign)), mean forward return, **$/trade**,
**total $ P&L**, #trades, after-cost Sharpe. The question it answers: *as I get more selective (smaller
top-%), does $/trade and precision improve?* — the conservative-application analysis. AUC and rank-IC are
reported for context, but the threshold curve is the headline.

## Discipline (so the numbers are trustworthy, not overfit)

Walk-forward only (no look-ahead), horizon-length purge, tradeable entry ≥09:35 ET + the $1 floor (from the
panel build), per-name cost net, and the predict-zero + within-timestamp-shuffle baselines reported
alongside (the threshold curve must dominate shuffle at every cut). A planted-signal sanity test
(`test_harness_pipeline.py`) proves the harness detects $ edge when edge genuinely exists, and a no-signal
panel collapses to ~chance.

## Demonstrated result (trusted-liquid daily, 2025-12-01..2026-06-17, top-500 ADV, $1M book)

See `experiments/harness_demo/`. The result is HONEST given the project's prior nulls: the headline 10%
L/S basket is ~break-even, but the **edge is real and concentrated in the tails** — precision and $/trade
rise monotonically as the cut tightens (GBM: 50%→1% cut, precision 0.510→0.520, $/trade $2→$563), and the
real curve dominates the shuffle baseline at every cut (shuffle AUC ~0.503). Ridge is the cleanest (Sharpe
2.0, +$458k at the top/bottom-2% cut). Runtime 1–6s for the whole train→apply→evaluate sweep over ~57k
rows × 13 features × 495 symbols.

Read a result as an idealized upper bound (frictionless basket, the universe-survivorship caveat the panel
carries), never a live realized P&L — the live `production_execution.py` layer measures the borrow /
capacity / partial-fill gap.

## Layering

`quantlib/harness/` depends on `quantlib/strategy_core/` (the shared decide-core + adapters + cost) and
`quantlib/battery/` (the panel loader + walk-forward), never the reverse. The live containers import the
same `strategy_core` — so there is exactly one home for the decision the harness validates and the
container runs.

---

## Related docs
Part of the [System Description](SYSTEM_DESCRIPTION.md) → *Strategy research harness & edge hunt*. See also:
[STRATEGY_BATTERY_RESULTS](STRATEGY_BATTERY_RESULTS.md) ·
[STRATEGY_BATTERY_PORTABILITY](STRATEGY_BATTERY_PORTABILITY.md) · [EXPLORATION_PIPELINE](EXPLORATION_PIPELINE.md) ·
[MODELLING_AGENT](MODELLING_AGENT.md) · the features it tests [FEATURE_PLATFORM](FEATURE_PLATFORM.md) · the
execution it ports to [STRATEGY_EXECUTION_ABSTRACTION](STRATEGY_EXECUTION_ABSTRACTION.md).
