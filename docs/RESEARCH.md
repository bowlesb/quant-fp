# Research Backlog — ML Approaches to Try

A deliberately large menu of approaches, because finding a real after-cost edge is
a search problem and we should sample the space broadly *with discipline*. This is
a backlog, not a plan: each item becomes an experiment only after the infra can
run it through the **same gauntlet** — purged/embargoed walk-forward, cost model
from our own quotes, replay-equivalence, and (for survivors) a frozen paper
campaign. Every experiment appends a verdict to `JOURNAL.md` and increments the
global experiment counter so we can deflate multiple-testing significance.

Ordering principle: **prior probability of a retail-accessible edge × cheapness to
test**. Cheap, high-prior items first. None of this matters until parity and data
quality are proven — a false positive from a leaky feature is worse than no edge.

## How each idea is specified

Every queued experiment is a config: `{universe, horizon, label, features, model,
cv, cost_model}`. The harness runs it and records OOS IC, after-cost Sharpe, the
sensitivity sweep, and feature importances. Items below name the knob they vary.

---

## Ring 1 — Label & horizon engineering (cheapest, highest prior)

Same features, change what we predict. Often worth more than new features.

1. **Horizon sweep** — fwd 30m / 2h / overnight / 2d / 5d excess return. Costs
   shrink with horizon; signal often decays slower. Prior: the edge, if any, lives
   at overnight–3d, not 30m.
2. **Volatility-scaled labels** — predict return / realized-vol instead of raw
   return, so the model isn't dominated by high-vol names.
3. **Rank / quantile-bucket labels** — train to predict cross-sectional decile
   rather than magnitude; matches how we actually trade (top/bottom decile).
4. **Triple-barrier labels** (López de Prado) — label by which of {profit target,
   stop, time} is hit first; turns the problem into calibrated classification.
5. **Sign-only classification** with probability calibration — sometimes more
   robust than regression; lets us threshold on confidence.
6. **Residual labels** — strip market/sector/size beta from the forward return and
   predict only the idiosyncratic residual (purer cross-sectional signal).

## Ring 1b — Conditioning / regime gating (knowing when *not* to trade)

7. **Dispersion gating** — only trade when cross-sectional return dispersion is
   high; cross-sectional strategies feast on dispersion.
8. **VIX / volatility-regime gating** — separate models or abstention by regime.
9. **Time-of-day conditioning** — open/midday/close behave differently; interact
   every feature with intraday bucket, or train per-bucket models.
10. **Liquidity gating** — abstain on names whose spread today exceeds a percentile
    of their own history (cost-aware trading).

## Ring 2 — Feature families (the box's compute earns its keep)

11. **Order-flow imbalance** (from trade_agg) — signed-volume z-scores, buy/sell
    pressure, large-print intensity. Strongest documented short-horizon family;
    already being collected.
12. **Quote microstructure** (from quote_agg) — spread dynamics, quote imbalance,
    quote-fade, depth changes.
13. **Short-horizon reversal** — multi-window returns (1/5/15/30m) and their
    interactions; classic retail-accessible mean-reversion.
14. **Cross-sectional momentum** — relative strength over 1–20 days vs universe.
15. **Overnight / auction structure** — close-to-open behavior, opening-auction
    imbalance, gap-fade vs gap-momentum conditioned on volume.
16. **Realized-vol & range features** — Parkinson/Garman-Klass estimators,
    intraday range percentiles.
17. **Cross-asset context** — SPY/QQQ/sector-ETF returns and beta; rates proxies
    (e.g. TLT) for rate-sensitive names; futures basis if available.
18. **Lead-lag features** — returns of systematically-leading symbols/sectors as
    predictors (built from a lead-lag graph fit offline).
19. **Event-anchored features** — days-until/since earnings, ex-div, index
    rebalance; interact with everything. Calendar data is cheap.
20. **News-derived features (later)** — minutes-since-headline, headline-burst
    intensity, embedding/sentiment as a *conditioner* (abstain near news), not a
    fast-reaction signal we can't win.

## Ring 3 — Model class diversity

21. **LightGBM** (committed champion) — rank:pairwise and regression objectives;
    monotonic constraints where economically justified.
22. **Other GBTs** — XGBoost / CatBoost as cheap challengers (ensemble diversity).
23. **Linear / ElasticNet baselines** — a must-have sanity floor; if GBT can't beat
    a regularized linear model OOS, the "edge" is probably overfit.
24. **Cross-sectional neural ranker** (3090) — MLP/DeepSets over the daily
    cross-section; permutation-invariant ranking.
25. **Sequence models** (3090) — temporal CNN / small Transformer / TCN on raw bar
    & order-flow sequences; challenger lane that must beat the GBT OOS to deploy.
26. **Conformal prediction** — calibrated abstention ("the model doesn't know"),
    turning uncertainty into a trade/no-trade gate.
27. **Regime-mixture / hierarchical models** — a meta-model allocating among
    sub-models by regime (ties to Ring 1b).

## Ring 3b — Ensembling & meta-learning

28. **Horizon ensemble** — combine 30m/overnight/multi-day signals; meta-weight by
    recent OOS performance.
29. **Stacking** — out-of-fold predictions of base models as features to a meta
    learner (with strict purging to avoid leakage).
30. **Bagging across seeds/subsamples** — variance reduction; stability of feature
    importances as an overfit diagnostic.

## Ring 4 — Different strategy species (same plumbing)

31. **Sector pairs / stat-arb** — cointegration scans across the universe
    (embarrassingly parallel on 32 threads); trade spread reversion.
32. **Crypto funding-rate carry** — long spot / short perp to harvest funding; a
    *known* structural carry rather than a hunted edge. High-prior Ring-4 item.
33. **Index-rebalance / event drift** — pre/post known-flow events.
34. **ETF-vs-constituents** — creation/redemption-driven dislocations.
35. **Options premium / wheel on ETFs (later)** — repackaged risk premium; treat
    skeptically, never as "free money."

## Cross-cutting methodology experiments (quality > cleverness)

36. **Purged & embargoed walk-forward** — verify no label-window overlap leaks;
    compare against naive CV to quantify the leakage it prevents.
37. **Cost-model sensitivity** — every survivor must hold under +50% spread and
    ±1-bar execution delay. Single-corner profits are noise.
38. **Feature-importance stability** — no single feature >40%; importances stable
    across retrains or we distrust it.
39. **Deflated Sharpe / multiple-testing correction** — track the global trial
    count; apply deflation so we don't celebrate the luckiest of N tries.
40. **Lookahead audit harness** — shift each feature's inputs forward one bar and
    confirm the label correlation behaves as expected (the parity test's sibling).

---

## First experiment wave (advisor proposal, 2026-06-10)

From a scoped research exploration tailored to our platform (SIP bars, second-scale
latency, ~$100k, cross-sectional decile L/S). Run only once we have data depth +
the modeling harness; each experiment gates the next; all after-cost, point-in-time
universe, purged+embargoed walk-forward.

**Best cost/latency fits to target first:** (C) overnight close→open drift (holding
period dwarfs our latency, amortizes spread — highest weight), and (B) intraday
continuation *confirmed by order flow* (signed-volume z-score, large prints). (A)
short-horizon reversal is real but is where the short book blows up — gate it with
news + vol regime. (E) quote imbalance is a cost/feasibility signal, NOT standalone
alpha at our latency.

**Two cheapest high-leverage changes** (do these before adding features): (1)
**vol-scale the label** (excess return / realized vol) so the model ranks alpha not
volatility; (2) add **cross-sectional signed-volume z-scores** + per-timestamp rank
transforms of continuous features.

**Validation must-dos (encode in the harness):**
- **Shuffle-label leakage canary** — a shuffled-label run must give IC≈0; if not, STOP.
- Embargo ≥ max(label horizon, feature lookback) — for 30m label + 60m features, ≥90m.
- Session-aware purging for the overnight label (don't let intraday test features
  use post-label data; respect day boundaries).
- Per-fold / per-timestamp standardization only (no full-sample scalers).
- Deflated Sharpe by trial count; a one-touch lockbox (recent 6–12mo) at the very end.
- Prefer IC stability (mean/std, sign consistency across folds) over peak IC.

**Concrete wave (E0 first — it's the gate):**
- **E0 baseline + leakage audit:** v1 features, LightGBM pointwise on vol-scaled 30m
  excess, per-ts standardized, embargo ≥90m. PASS = mean OOS IC>0, sign-consistent
  ≥80% folds, AND shuffle-label canary ≈0. (Most important run in the wave.)
- **E1 label engineering:** sweep {raw, vol-scaled, rank} × {regression, lambdarank}.
  PASS = best beats E0 after-cost IC by ≥0.005 with deflated Sharpe still ≥1.
- **E2 order-flow features:** add signed-volume z (5/15/30m), vol-normalized returns,
  rank transforms, late-day flow. PASS = +0.005 after-cost IC vs E1, new features
  carry real SHAP weight; report long/short IC separately.
- **E3 overnight book (own model):** label = vol-scaled overnight excess; late-day +
  close-vs-VWAP + distance-to-close features; session-aware purge. PASS = standalone
  after-cost Sharpe≥1, IC≥0.02 t≥4; check decile-overlap <~0.6 vs 30m (non-redundant).
- **E4 news as a gate (not signal):** suppress reversion shorts into fresh news.
  PASS = equal/better short-book IC with materially lower left-tail/drawdown.

**Explicitly NOT in wave 1:** triple-barrier labels, NLP sentiment embeddings,
sequence/deep models, quote-imbalance-as-primary — low EV at our latency or high
overfit surface before the baseline is proven clean.

Full distilled rationale + feature tiers in JOURNAL (2026-06-10 advisor entry).
