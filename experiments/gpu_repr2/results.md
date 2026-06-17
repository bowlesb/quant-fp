# GPU repr-2 — non-linear cross-asset behavioral embedding (results)

**Lane:** financial world-views — models of how names *behave*, in a learned embedding space.
**GPU:** RTX 3090 via `fp-torch-gpu` (torch 2.3.0, CUDA verified). Lock held + released per `GPU_QUEUE.md`.
**Substrate:** REAL certify300 daily panel — **2,722 symbols × 377 days** (≥250-day filter), well-powered for
cross-sectional representation learning (NOT the n=137 tick model the queue correctly rejected).

## What this builds on (#76) and why it's not redundant

#76 (`experiments/gpu_repr`) learned a **linear (SVD), single-channel (close-to-close return correlation)**
per-symbol embedding. It validated OOS (held-out cohesion 0.092 vs 0.0003 random) and shipped as the
`symbol → cluster_id` map that `peer_relative` reads. The #76 INSIGHTS doc states this linear/single-channel
embedding is *"the floor, not the ceiling"* — any non-linear edge must live beyond it. This lane tests
exactly that: a **non-linear, multi-channel** embedding that must BEAT the linear baseline on its own OOS
metric to justify its complexity.

## Method

**Richer substrate (5 channels):** per (symbol, day) decompose behavior into `overnight` (gap),
`intraday` (session drift), `c2c` (= the #76 channel), `logdvol` (activity level), `dvol_chg` (activity
dynamics). Each channel is **cross-sectionally z-scored per day** (removes the market factor → pushes the
embedding toward style/peer structure). Per-symbol summary = mean/std/downside-mean/lag-1-autocorr + top-4
daily-PCA loadings per channel = 40 features.

**Non-linear model (GPU):** a contrastive autoencoder. Encoder MLP (40→256→128→16, GELU+LayerNorm),
symmetric decoder. Supervision: on each train day, positives = same c2c-return decile (co-moved), negatives
= different decile; InfoNCE pulls co-movers together, plus an MSE recon term to keep the embedding a faithful
compression. **Held-out time:** fit on the first 70% of days; cohesion evaluated only on the last 30%.

**OOS metric (identical to #76):** KMeans(11) on the embedding → within-minus-across-cluster pairwise
c2c-return correlation on the held-out-time window. Random labels = floor.

## Results — honest, multi-seed (5 seeds)

| arm | held-out cohesion (mean ± std) | vs baseline |
|---|---|---|
| **A** — #76 baseline (c2c-SVD), reproduced | **0.114 ± 0.004** | — |
| **B** — multi-channel **linear** PCA | **0.095 ± 0.007** | **worse** |
| **C** — multi-channel **contrastive AE** (GPU) | **0.131 ± 0.002** | **+0.017 (+15%)** |

- **The AE wins all 5 seeds** (min lift +0.010; AE's worst seed 0.129 > baseline's best 0.119). Not seed noise.
- **The extra channels alone don't help** — linear multi-channel (B) is *worse* than plain c2c-SVD. **The
  non-linearity is what carries the lift**, combining channels (e.g. gap-direction × intraday-fade ×
  volatility regime) that linear SVD on c2c cannot represent.
- Random-label cohesion ≈ 0.000 on the held-out window (floor intact).

**Non-redundancy vs the #76 linear embedding it must beat:**
- Adjusted Rand index between AE clusters and #76 clusters = **0.25** (low → genuinely different partition).
- Canonical correlations (AE vs #76 SVD coords) decay **0.95 → 0.91 → 0.84 → 0.70 → 0.51 → … → 0.31**: only
  the top ~3 dims overlap with the linear baseline; dims 4–8 carry structure the linear embedding does NOT.
  The AE is **not** a rotation of #76.

**What the new structure is (economic read of nearest neighbours):** the AE forms cleaner
volatility/momentum-*regime* clusters that #76's pure-co-movement clustering blurs —
NVDA→{AMD, AVGO, PLTR, TSM, MU} (clean semis/AI vs #76 mixing in power-infra ETN/VRT);
TSLA→{MSTR, COIN, AMD, SMCI, NVDA} (a high-vol speculative-momentum cohort GICS/c2c-SVD don't form cleanly).
This is behavior-by-how-it-trades, the structure this lane is for.

## Artifacts (committed under `experiments/gpu_repr2/`)

| file | what |
|---|---|
| `build_profiles.py` | 5-channel daily profile builder (deterministic, parity-trivial) |
| `train_embedding.py` | 3-arm trainer (baseline / linear-multichannel / contrastive AE) |
| `robustness.py` | 5-seed stability + ARI + canonical-corr non-redundancy harness |
| `build_cluster_map.py` | fits the production AE map on full history (the deployed nightly lookup) |
| `out/profiles.npz` | the 2722×377×5 cross-sectionally-standardized panel |
| `out/embedding_result.json` | single-seed 3-arm result |
| `out/robustness_result.json` | the load-bearing multi-seed + non-redundancy numbers |
| `out/behavioral_embedding.npz` | AE / PCA / baseline embeddings + symbols |
| `out/behavioral_clusters_v2.parquet` | **shippable** `symbol → cluster_id` map (schema == v1) |
| `out/ae_cluster_map.npz` | production embedding + labels + feature names (provenance) |

## Feature-candidate verdict (the feature bar, applied HONESTLY)

**SHIP candidate — AE cluster map as a drop-in UPGRADE to `behavioral_clusters_v1`** (read by the existing
`peer_relative` feature). This is the honest, non-redundant move:
- `peer_relative` peer-demeans intraday returns against a `symbol → cluster_id` map. A **higher-OOS-cohesion**
  cluster map = a cleaner shared/common component = a cleaner idiosyncratic residual. The AE map scores +15%
  OOS cohesion and has **no degenerate tiny cluster** (sizes 153–503 vs v1's 51–454; a 51-name peer mean is
  noisy). So this is a direct, on-objective improvement to an *already-shipped* feature — it adds **zero new
  feature columns** (no count bump), it makes the existing column better.
- It is NOT a new redundant feature: `peer_relative` / `cluster_id` already exploit #76's embedding; this
  REPLACES the lookup behind them with a better one (ARI 0.25 → it actually changes the grouping).
- **Promotion path (coordinator-owned, this lane never touches the live tree):** swap
  `behavioral_clusters_v1.parquet` → v2 behind `peer_relative`, then run the standard parity sweep +
  shuffle-canary + per-symbol-demean + **OOS-IC on real labels**. The cohesion win is necessary but NOT
  sufficient — only the OOS-IC on the actual return target decides whether the better grouping translates to
  feature value. **Do not deploy on the cohesion number alone.**

**DO NOT ship — standalone AE embedding coordinates (the old C2 candidate).** Honest call: these are
near-constant within a day (a learned style/sector control, not alpha), and #76 already judged the SVD-coord
version low-value. Shipping 16 slow per-symbol coord columns would inflate the feature count with redundant,
likely-noise features. Rejected.

**Net:** one genuinely additive, non-redundant outcome (a better cluster map, zero count bump), and an
honest rejection of the tempting-but-redundant coordinate features.
