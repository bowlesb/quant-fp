# W7 — Gradient-boosting NON-LINEAR predictor → LIQUID portfolio L/S (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Lens L3 (ML). Friction-wall design: PORTFOLIO L/S (diversified) +
LIQUID universe + a model trained to predict a multi-day (low-turnover) forward return. The ML angle: a
gradient-boosted tree can find NON-LINEAR feature interactions a linear cross-sectional IC misses — cycles
1-2 only tested linear/single signals, so a non-linear combination is genuinely untested.

## Hypothesis

A LightGBM model trained (walk-forward) to predict the 5-day forward cross-sectional return rank from a
panel of price/volume/volatility features, turned into a decile LONG/SHORT portfolio on the LIQUID universe,
earns a positive net-of-cost portfolio return OOS — i.e. a non-linear feature combination clears the friction
wall where individual linear signals did not.

## Universe + data + features (self-contained, no store-internal deps)
- /store/raw/bars 126 days. LIQUID universe = top ~500 by median daily dollar-volume (+ top-100 megacap).
  UTC-correct daily close/open (RESEARCH_PITFALLS #1).
- FEATURES (computed point-in-time from bars, per (symbol, date), all trailing — no leakage): trailing
  returns over {1,2,3,5,10,21,42,63}d; realized vol over {5,10,21}d; high-low range / Garman-Klass vol;
  dollar-volume + its trend; distance from {5,10,21,63}d moving average; the overnight vs intraday split;
  a short-term-reversal (1-day) and momentum (skip-1) pair. ~25-35 features. (A v2 could add the 606 live
  features — deferred to keep W7 self-contained and fast.)
- LABEL: forward 5-day cross-sectional return RANK (predict relative, not absolute — the portfolio is L/S so
  only cross-sectional ordering matters). Also test a 10-day label.

## Model + validation (the anti-overfit discipline is everything for ML)
- LightGBM (CPU, fp-ml image), modest depth (3-6) + strong regularization + early stopping — a tree ensemble
  WILL overfit 126 days if unconstrained.
- WALK-FORWARD (expanding or rolling): train on dates < T, predict dates in [T, T+gap], step forward. NEVER
  train and test on the same dates. A gap between train and test ≥ the label horizon (5-10d) to avoid label
  leakage across the boundary.
- The prediction → decile L/S portfolio, equal-weight, rebalanced every 5 days (low turnover), LIQUID only.
- GATES: the OOS (walk-forward, never-trained-on) portfolio net-of-cost return is the ONLY number that
  matters — in-sample fit is meaningless for a GBM. Per-rebalance bootstrap CI (non-overlapping rebalances,
  excludes zero above), measured-spread cost + 2×, per-symbol demean of the label, a shuffle-canary
  (permute the label within each training cross-section → the model should learn nothing → OOS net ≈ 0).
- FEATURE IMPORTANCE: report the top features by gain — these become candidate new features for the platform
  (the ML-feature-discovery payoff) REGARDLESS of whether the L/S itself clears cost.

## Expected / confidence
- Confidence the walk-forward OOS LIQUID L/S nets positive with bootstrap CI > 0: **~25%.** A GBM can find
  real non-linear structure, but (a) 126 days is very thin for ML (huge overfit risk — the walk-forward +
  canary are the guards), (b) the underlying price features are the same ones that failed linearly, so the
  non-linearity must add genuinely new predictive power, (c) cost still applies. Pre-commit the prior. The
  feature-importance output is valuable EVEN IF the L/S fails (it tells us what the platform's strongest
  raw predictors are).
- KEEP-AS-LEAD: walk-forward OOS LIQUID L/S net positive, bootstrap CI > 0, canary-clean (shuffled label →
  ~0) → an ML strategy spec + the top features as feature proposals. AMBIGUOUS: OOS positive but
  CI-marginal. KILL: OOS net ≤ 0 or the shuffled-label canary also looks positive (overfit).

## Friction-wall scorecard
[portfolio-diversified ✓] [liquid ✓] [low-turnover ✓ 5d rebalance] [ML-nonlinear — new] — and the
feature-importance is a guaranteed deliverable (what predicts, non-linearly) feeding wave 2.

## GPU note
W7's LightGBM runs on CPU (fp-ml, fast for this tabular size). The GPU (RTX 3090) is reserved for W8
(deep autoencoder feature-discovery), which genuinely benefits — keeping the GPU for where it adds value.
