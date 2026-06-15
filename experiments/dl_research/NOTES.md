# DL Research Prototype — Notes & Handoff

> Status: RESEARCH / scratch (2026-06-14). Branch `research/dl-prototype`.
> Goal: stand up the deep-learning harness for the `(minute × ~10k symbol × 519 feature)` cross-sectional
> panel NOW, against the FeatureStoreClient contract, so synthetic→real is a one-line swap. Does NOT touch
> platform code (`quantlib/`, `services/`, `db/`, `rust/`).

This directory is self-contained:

| File | Purpose |
|------|---------|
| `feature_catalog.csv` / `feature_meta.json` | The platform's **real 519 feature names** + metadata, exported from `quantlib.features.registry.REGISTRY.catalog()` via the fp-dev read-only boundary. |
| `synth_panel.py` | Generates a realistic synthetic panel with a **planted cross-sectional signal** (5 real features predict; ~510 are noise) + sector structure + a regime variable + 4 forward-return labels. |
| `loader.py` | `SyntheticFeatureStoreClient` — **same signatures as `quantlib.modelling.client.FeatureStoreClient`** (`catalog()`, `get_features()`, `training_export()`). Swap to real = one line. |
| `evaluation.py` | Walk-forward **purged + embargoed** splits; **rank-IC / ICIR per horizon**. |
| `model.py` | Rank-1 DL: **PLR numerical embeddings → shallow MLP trunk → 4 horizon heads → cross-sectional ranking loss**. |
| `train.py` | End-to-end harness: synth panel → loader → walk-forward → {LightGBM, Rank-1 DL} → rank-IC/ICIR. |
| `results.json` | Latest run metrics. |

Run: `.venv/bin/python synth_panel.py && .venv/bin/python train.py`  (add `--quick` for a fast smoke run).

---

## 1. Actionable design choices (from the paper survey)

The agenda's ranking is confirmed by the literature. Concrete recipe to implement:

### (a) Numerical embeddings — the #1 lever (Gorishniy 2022, *On Embeddings for Numerical Features*, NeurIPS)
- **PLR** = per-feature `ReLU(Linear(Periodic(x)))`, where `Periodic(x) = [sin(2π·c·x), cos(2π·c·x)]`
  with **trainable** frequencies `c ~ N(0, σ²)`. Each feature gets its OWN independent embedding (no sharing).
- **σ is THE key hyperparameter** (paper §4.7): tune `LogUniform[0.01, 1.0]`; ~50% of tasks optimal below
  σ≈0.05. **Start σ=0.05 and sweep.** `k` frequencies ~ `[1,128]`; embedding dim per feature ~ `[1,128]`.
- At 519 features: per-feature embeddings are **concatenated**. With dim `d`, trunk input = `519·d`.
  Keep `d≈8–16` so the flat vector stays ~4k–8k. (Our prototype: d=12 → 6,228-dim, k=24.)
- Runtime overhead is small even though param count balloons (paper Table 7: ~2000× params → ~1.5× runtime).
- PLE (piecewise-linear, quantile/tree bins, scale-invariant `[0,1]` output) is the alternative; PLR is the
  best average performer. **Implemented PLR in `model.py`.**

### (b) Trunk — shallow MLP, NOT FT-Transformer at our width
- FT-Transformer attention is **O(n_features²)**; with 519 features the seq length is ~520 tokens and the
  paper *explicitly warns* it "may not be easily scaled when n_features is too large" — exactly our regime.
- **Lead with MLP + PLR** (the 2022 paper shows MLP-PLR reaches parity with attention models).
- **Gu/Kelly/Xiu 2020:** NN performance **peaks at ~3 layers** in low-SNR finance (NN3 > NN5), and
  unregularized OLS *lost money* (−3.46% R²). → shallow trunk `(512, 256)`, BatchNorm + dropout 0.2 +
  weight decay + early stopping + seed ensembling. Reserve FT-Transformer for a later comparison.

### (c) Cross-sectional attention — MASTER (Li/Liu, AAAI 2024)
- **The batch dimension IS the stock dimension** — one prediction timestamp = one batch = the whole
  cross-section. Module order: market-guided gating → feature linear+posenc → **intra-stock temporal
  attention (per stock)** → **inter-stock attention (across stocks, at each timestep)** → temporal
  aggregation → linear head.
- **Market-guided gating:** a market-state vector `m` drives `α(m)=F·softmax_β(W·m+b)`, then
  **Hadamard-rescales each stock's features** — built-in regime conditioning. Our **breadth /
  market-context / beta** features feed this perfectly.
- Loss in the paper is **MSE on cross-sectionally z-scored returns** (so MSE ≈ ranking). Reported CSI300
  IC 0.064 / RankIC 0.076 / IR 2.4.
- **Adaptation:** our panel is minute snapshots (no τ-window per row by default). Directly usable pieces:
  (1) batch = the cross-section at one minute, (2) inter-stock attention across stocks at each minute
  (subsample the cross-section — 10k is large for full O(N²); sample N≈512–2048/batch), (3) the
  market-guided gate from breadth/market-context features. **This is Rank-2 — after Rank-1 clears the bar.**

### (d) Ranking loss — cross-sectional, multi-horizon
- **Per minute, per horizon:** z-score predictions and labels across the cross-section; loss =
  **−mean(Pearson-IC)** (a differentiable rank-IC surrogate). Sum over the 4 horizons. **Implemented in
  `cross_sectional_rank_loss` in `model.py`.**
- Upgrades available: **soft-rank-IC** (differentiable sorting / pairwise sigmoid) or **ListNet**
  (softmax-over-preds vs softmax-over-labels cross-entropy) for a true listwise objective.
- **Make the per-minute cross-section the batch unit** so each loss term is a within-minute ranking.

### (e) Multi-horizon heads
- Shared trunk → **4 small linear heads** (5/15/30/60-min). Equal-weight the per-head ranking losses to
  start. Heads are cheap; the trunk does the work.

### (f) SSL pretext tasks for Phase 2 (VIME 2020 / SubTab 2021)
- **SubTab (recommended primary):** split the 519 columns into **K=4 overlapping subsets at 75% overlap**;
  shared encoder per subset; **decoder reconstructs the FULL 519 features** from each subset; mean-aggregate
  the K embeddings at inference. Recon (MSE) + optional distance loss; **keep contrastive low/off** because
  adjacent minutes are near-duplicates (contrastive false negatives).
- **VIME-self (fast comparison):** mask `Bernoulli(0.3)`, corrupt by resampling masked entries from each
  column's marginal (swap-noise); two heads — mask-estimation (BCE) + reconstruction (MSE), `α=2.0`.
- **Why our platform makes this defensible:** the parity guarantee means the unlabeled pretraining corpus is
  genuinely leakage-free, so SSL doesn't silently bake leakage into the representation (the classic way SSL
  finance papers fool themselves).

### (g) Finance eval protocol + low-SNR cautions (Gu/Kelly/Xiu 2020)
- **OOS R² benchmarked against ZERO**, not the historical mean: `R²_oos = 1 − Σ(r−r̂)²/Σr²`.
- **Walk-forward:** expanding train, rolling validation, untouched OOS test; refit periodically; **never
  shuffle time**. We additionally **purge** (drop the last `max_horizon` train minutes that overlap the
  test window) and **embargo** (buffer after test) — implemented in `evaluation.py`.
- **Economic eval:** decile long-short portfolios per horizon, annualized Sharpe (their NN: 1.35 VW / 2.45 EW).
- **Expect tiny numbers:** best monthly R² ~0.3–0.4%. Regularize heavily; ensemble seeds. Dominant real
  signals: momentum/reversal, liquidity, volatility.

---

## 2. Pretrained-weights survey — honest verdict

**Confirmed: none of the time-series foundation models can be the predictor on our cross-sectional problem.**
Reading their actual input contracts:

| Model | HF / size | License | Input contract | Cross-sectional? |
|-------|-----------|---------|----------------|------------------|
| **Chronos / Bolt / Chronos-2** | `amazon/chronos-*`, 8M–710M | **Apache-2.0** | strictly **univariate** context array; "groups" = a few correlated channels → one target | No |
| **Moirai / MoE / 2.0** | `Salesforce/moirai-*`, 14M–311M | **CC-BY-NC** (weights) — non-commercial blocker | "any-variate" flattens a few correlated variates → one forecast | No |
| **MOMENT** | `AutonLab/MOMENT-1-*`, 40–385M | **MIT** | `[batch, n_channels, ctx=512]`, **channel-independent** (no cross-channel attn) | No |
| **TimesFM** | `google/timesfm-*`, 200–500M | **Apache-2.0** | decoder-only **univariate**, independent series | No |
| **Lag-Llama** | `time-series-foundation-models/Lag-Llama`, ~2.5M | **Apache-2.0** | strictly **univariate** autoregression | No |
| **TabPFN v2** | `Prior-Labs/TabPFN-v2-*`, ~7–11M | Apache-2.0 + attribution | in-context `(X_train,y_train)+X_test`, **caps ~10k rows / ~500 feats** | Yes (architecturally) — but see limits |

**Verdict:**
- **TSFMs (Chronos/Moirai/MOMENT/TimesFM/Lag-Llama): cannot be the predictor.** They all model a single
  series' (or a handful of correlated channels') own future along the time axis. There is an **axis mismatch** —
  they consume "one series over a long window," we need "one timestamp across a wide cross-section." None has
  a cross-sectional ranking objective. **This confirms the agenda's claim.**
- **Only legitimate use = frozen auxiliary embedding.** **MOMENT** (MIT, 1024-d per-series embedding) computed
  per asset from its own raw price history, fed as a handful of *extra features* into OUR cross-sectional
  ranker. The cross-asset comparison is still done by our model. (Chronos/TimesFM could similarly contribute a
  per-asset forecast scalar.) Low priority; revisit after Rank-1/2 clear the bar.
- **Licensing:** Moirai weights are **CC-BY-NC** (non-commercial) → disqualified for live trading. Chronos /
  MOMENT / TimesFM / Lag-Llama are clean (Apache-2.0 / MIT).
- **TabPFN v2** is the only *architecturally* cross-sectional candidate, but our dims (~519 features > ~500 cap;
  ~10k rows/cross-section = the documented cap, and it's O(N²) in context) sit **at or past both guardrails** —
  expect heavy VRAM and subsampling. Worth a *bounded* experiment (override limits / drop features / subsample
  support), or evaluate the newer **TabPFN-2.5** (~50k rows / ~2k feats — a much better shape fit; check license).
  Not a clean out-of-the-box predictor at our scale.

**Bottom line: pretrain our OWN small SSL encoder on the parity-clean panel (Phase 2); use off-the-shelf TSFMs
only as optional frozen per-asset features.**

---

## 3. GPU / environment

- **GPU:** NVIDIA GeForce **RTX 3090 (24 GB)**, compute capability sm_86. Driver 535.309.01 / CUDA 12.2.
- **Torch:** `torch 2.5.1+cu121`, `cuda.is_available() == True`. (cu121 wheels run fine on the 12.2 driver.)
- **Env:** dedicated scratch venv at `experiments/dl_research/.venv` (Python 3.12). Deps: torch (cu121),
  lightgbm 4.6, polars 1.41, numpy, pandas, scipy, scikit-learn, pyarrow. **Does NOT pollute fp-dev.**
  (fp-dev / fp-gpu images have no torch; torch lives only in this scratch venv.)
- The platform feature registry is read read-only via the fp-dev image (the modelling boundary):
  `docker run --rm -e DB_PASSWORD=test -v "$PWD":/app -w /app fp-dev python -c "from quantlib.features.registry import REGISTRY; ..."`.

---

## 4. Toy results — did DL recover the planted signal, and beat/lose to GBDT?

<!-- RESULTS_PLACEHOLDER -->

---

## 5. 3090 feasibility

<!-- FEASIBILITY_PLACEHOLDER -->

---

## 6. "What we need when real data arrives" — handoff checklist

The harness is built so the synthetic→real swap is one line. To go live:

1. **Swap the client** (`loader.py`): replace
   ```python
   client = SyntheticFeatureStoreClient.from_parquet("synth_panel.parquet")
   ```
   with
   ```python
   from quantlib.modelling.client import FeatureStoreClient
   client = FeatureStoreClient(store_root=<store>, val_root=<val>)
   ```
   `catalog()`, `get_features()`, `training_export()` signatures already match. Use
   `training_export(names, start, end, min_grade="B")` to get the **certified-only, settled** training set
   (so the model never trains on a feature production can't reproduce). Drop features the call reports as
   not-certified, or file a feature request.

2. **Label construction** (the labels are NOT in the feature store — build them):
   - For each (symbol, minute), compute forward returns at +5/+15/+30/+60 min from a tradeable price.
   - **Use a tradeable entry** (≥09:35; never the 09:30 open print — see the gap-fade look-ahead trap).
   - Align labels so the label at minute `t` uses only prices at `t+h` (no peeking); store as
     `fwd_ret_{5,15,30,60}m`. Cross-sectionally z-score (or rank) per minute before the ranking loss.

3. **Walk-forward protocol** (already in `evaluation.py`): expanding train, rolling test, **purge =
   max_horizon (60 min)**, **embargo ≥ feature memory** (multi-day features need a multi-day embargo — bump
   `embargo` accordingly). Refit per fold. Report **rank-IC + ICIR per horizon**, then decile long-short
   Sharpe **net of cost** as the economic gate.

4. **Scale knobs for 10k symbols** (vs 500 synthetic): batch = per-minute cross-section; for Rank-2 (MASTER)
   subsample N≈512–2048 symbols per cross-section to fit 24 GB O(N²) attention. Rank-1 MLP is per-row so it
   scales linearly — no problem at 10k.

5. **Trust-grade feature selection:** use `catalog()`'s `status`/`value_grade` to weight or filter features
   (a lever no off-the-shelf model has). Down-weight C-grade, drop divergent.

6. **Sanity:** confirm the loader's `panel_to_arrays` minute/symbol indexing matches the real frame's
   `(symbol, minute)` ordering, and that NaN policy is handled per the registry's `nan_policy` column
   (synthetic data has none; real data will).

### Still unknown until real data
- **Real SNR.** Synthetic signal is planted; real rank-IC will be far smaller (expect single-digit-bps R²).
  The planted-signal test only proves the harness *can* recover an edge — not that one exists.
- Whether DL **beats GBDT net of cost** on real data (the actual gate). On synthetic the point is recovery +
  plumbing, not a verdict.
- Real feature **NaN density / coverage** and how many features survive the `min_grade="B"` certification gate.
- Whether **cross-sectional attention (MASTER)** adds over the MLP on real data — only testable once Rank-1
  clears the bar.
