# GPU Representation-Learning Lane — Plan + First Result

> Lane: **financial WORLD-VIEWS** — models of financial BEHAVIOR in different EMBEDDING SPACES.
> GPU: idle RTX 3090 (24 GB, sm_86, driver 535 / CUDA 12.2). Torch 2.5.1+cu121, `cuda.is_available()==True`.
> Worktree: `research/gpu-repr-learning` at `/home/ben/quant-fp-wt-gpu-repr` (NEVER edits the live tree).
> Status: 2026-06-16. Substrate = synthetic panel (planted structure) + REAL certify300 daily bars (5,223 symbols × 18mo).

This lane is **distinct from and complementary to** the prior `experiments/dl_research/` work, which is a
**supervised cross-sectional RETURN RANKER** (PLR→MLP→ranking loss). This lane does **unsupervised /
self-supervised REPRESENTATION learning**: learn the latent structure of financial behavior, then mine
the embeddings for **feature candidates** (findings→features). We do NOT claim a tradeable edge — we claim
(a) honest structure insight and (b) parity-true feature candidates that face the same OOS/canary bar as any feature.

---

## 0. Why this lane (the hypothesis)

Scoreboard: linear liquid factors are mostly arbitraged-away; the one edge (W11 overnight-beta) is
**regime-conditional** (pays when cross-sectional dispersion is high). That regime-conditionality is itself a
hint that **non-linear / regime structure is real and relevant**. Linear factor tests cannot see structure
that lives in a learned manifold. This lane builds embedding spaces to (a) reveal that structure and
(b) emit it as features the all-features model can consume.

---

## 1. Infra decision: reuse the working torch venv, defer the container

The prior lane already stood up a **working torch+CUDA venv** at
`/home/ben/quant-fp/experiments/dl_research/.venv` (torch 2.5.1+cu121, CUDA verified on the 3090, plus
lightgbm/polars/numpy/sklearn). It runs GPU training directly on the host — no container needed to USE the GPU.

**Decision:** prototype on this venv now (fastest path to a result). In parallel, a reproducible
**`fp-torch` GPU image** (`docker/fp-torch.Dockerfile`) is provided for the eventual coordinated path —
because the *inference* half of any shipped feature must run inside the platform's image lineage, not a
scratch venv. The training half can stay on the venv; the **inference half must be a deterministic
learned `FeatureGroup`** living in `quantlib/features/groups/` and running in the normal `fp-dev`/capture
image (CPU, sub-ms) — see §4. Torch is NOT required at inference if we export weights to numpy (preferred).

`nvidia-smi` access from a container is verified separately (`docker run --gpus all`); the venv path needs
no container and is already proven.

---

## 2. The five embedding directions (prioritized) + pre-registration

Each direction lists: input, model, the **insight** it yields, the **feature candidates** it emits, whether
those candidates are **real-time-feasible**, and the **pre-registered "good" bar** (decided BEFORE training).

### D1 — Feature-vector VAE / Autoencoder → market-STATE embedding  ★ FIRST PROTOTYPE
- **Input:** the (symbol-minute × 519-feature) panel row (one market state).
- **Model:** β-VAE, encoder `519→256→128→z`, `z∈{8,16,32}`; symmetric decoder; Gaussian recon + KL.
- **Insight:** does a low-dim latent organize market states by sector / regime / volatility? Is the latent
  manifold smooth and interpretable, or memorized?
- **Feature candidates:** (i) the `z` latent dims themselves; (ii) **reconstruction error** per row
  (an "anomaly / unusual-state" scalar — states the model finds surprising); (iii) distance-to-nearest
  cluster centroid in `z`.
- **Real-time-feasible:** YES. Inference = one feedforward pass over the current 519-vector → deterministic.
  Export encoder weights to numpy; runs sub-ms on CPU in the FeatureGroup. **This is the headline RT candidate.**
- **Pre-registered bar (MUST pass to call it "learned, not memorized"):**
  1. **Held-out SYMBOLS:** train on 80% of symbols, reconstruction R² on the held-out 20% must be within
     **20% relative** of train R² (no symbol-identity memorization).
  2. **Held-out TIME:** train on the first 80% of minutes, recon R² on the last 20% (post-embargo) within 20% rel.
  3. **Beats PCA baseline:** at equal latent dim, VAE recon R² ≥ PCA recon R² (else linear is enough — use PCA, simpler & parity-trivial).
  4. **Latent is structured, not collapsed:** ≥ 75% of latent dims have KL > 0.01 (no posterior collapse);
     silhouette of sector labels in `z` > silhouette in a random projection of equal dim.

### D2 — Contrastive / SSL on feature sequences → behavioral embedding
- **Input:** short windows of a symbol's feature/price sequence.
- **Model:** SubTab-style (per NOTES.md §1f) — split 519 cols into K overlapping subsets, shared encoder,
  reconstruct full vector; contrastive kept OFF/low (adjacent minutes are near-duplicates → false negatives).
- **Insight:** do symbols/states that *behave* alike cluster regardless of sector label?
- **Feature candidates:** embedding coords; nearest-behavioral-neighbor agreement.
- **RT-feasible:** YES (encoder feedforward). Priority: AFTER D1 clears its bar.

### D3 — Sequence / world model (LSTM/temporal-transformer) on intraday → DYNAMICS
- **Input:** per-symbol ordered minute sequence.
- **Model:** small LSTM next-state predictor; hidden state = embedding; **prediction surprise** = feature.
- **Insight:** learn market dynamics; where is the system predictable vs surprising?
- **Feature candidates:** **next-state prediction surprise** (||pred − actual||) — a dynamics-anomaly scalar;
  hidden-state coords.
- **RT-feasible:** PARTIAL — needs a rolling hidden state per symbol (stateful). Feasible **only** inside the
  platform's coherent FeatureState state-manager (state_spec/seed/fold/emit) so backfill == stream by
  construction. Flag as RT-feasible-WITH-state, parity-critical. Priority: 3rd.

### D4 — Stock embeddings (word2vec-for-stocks) on REAL daily bars → peer/lead-lag structure  ★ REAL DATA
- **Input:** REAL certify300 daily returns (5,223 symbols × 18mo) — co-movement, lead-lag, regime response.
- **Model:** factorize the symbol×symbol co-movement / lead-lag matrix (or skip-gram over "which stocks
  moved together today") → a dense per-symbol vector.
- **Insight:** **data-driven peer groups / sectors / lead-lag networks** — REAL, not synthetic. Compare
  discovered clusters to GICS sectors; find lead-lag pairs.
- **Feature candidates:** per-symbol static embedding coords (slow-moving → trivially parity-true, recomputed
  nightly); **distance-to-peer-centroid return** (a relative-strength-vs-behavioral-peers feature).
- **RT-feasible:** YES (embedding is a static per-symbol lookup refreshed nightly; the *derived* feature
  like peer-relative return is a cheap RT compute). High value because it's REAL data. Priority: 2nd
  (parallel to D1 — uses different data + CPU/GPU-light, runs concurrently).

### D5 — Regime embeddings → discover regimes (W11 dispersion-conditionality is the live hook)
- **Input:** per-minute market-state vector (breadth, dispersion, VIX-proxy, market-context features).
- **Model:** cluster / GMM / VAE over market-aggregate states → regime id + soft regime weights.
- **Insight:** do data-driven regimes recover the W11 "high-dispersion pays" condition? Is regime structure
  stable OOS?
- **Feature candidates:** **regime-id one-hot + soft regime membership** (a market-context feature shared
  across symbols) — directly feeds regime-conditional strategies like W11.
- **RT-feasible:** YES (cluster-assign the current market vector — deterministic, sub-ms). Priority: 4th,
  but highest *strategic* relevance given W11. Builds on D1's latent.

---

## 3. Rigor protocol (applies to EVERY direction)

- **Generalization is the whole game.** A memorizing autoencoder is worthless. Every model validates on
  **held-out symbols AND held-out time** (purged + embargoed, reusing `dl_research/evaluation.py` splits).
- **PCA / linear baseline first.** If a linear method matches the deep model on the pre-registered metric,
  ship the linear one (simpler, parity-trivial). Deep learning must EARN its complexity.
- **Pre-register "good"** before training (done above, per direction). No moving goalposts.
- **Derived features face the full feature bar:** shuffle-canary (label-permutation IC ≈ 0),
  per-symbol-demean (survives removing symbol fixed-effects), and OOS IC — same gate as every platform feature.
  An embedding feature gets NO free pass for being fancy.
- **Honest reporting:** synthetic results prove the *harness recovers structure*, NOT that the structure
  exists in real markets. D4 (real bars) is where real-structure claims are allowed.

## 4. Path to shipping a feature (findings→features, parity-true)

The model trains OFFLINE on the GPU. To SHIP a candidate:
1. **Export weights to numpy** (`.npz`) — no torch at inference. Encoder becomes a pure-numpy feedforward.
2. **Implement a deterministic `FeatureGroup`** in `quantlib/features/groups/` that loads the `.npz` and
   computes the feature from the live 519-vector (D1/D2/D5 = stateless feedforward; D3/peer = stateful via the
   FeatureState manager so backfill==stream).
3. **Parity/canary/trust pipeline:** the feature goes through the normal nightly parity sweep, shuffle-canary,
   OOS-IC, and trust-grade registration. The **coordinator runs the coordinated deploy** — this lane does NOT
   touch the live tree.
4. **Versioning:** adding features bumps the feature-set version (see CLAUDE.md feature-count assertion).

## 5. Deliverables status

- [x] Plan (this doc) + per-direction pre-registration.
- [x] `fp-torch` GPU Dockerfile (reproducible image for the eventual coordinated inference path).
- [x] **D1 first prototype: β-VAE on the feature panel** — `models/vae.py` + `train_vae.py`, with the
      pre-registered held-out-symbol / held-out-time / PCA-baseline / collapse checks built in.
- [x] First-result analysis written to `out/` + summarized in this lane's report.
- [ ] D4 stock embeddings on real bars (next).
- [ ] Promote the best RT-feasible candidate to a FeatureGroup PR for the coordinator.
</content>
