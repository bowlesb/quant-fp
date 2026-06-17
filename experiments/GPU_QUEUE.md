# GPU job QUEUE — owned by Modelling Agent (MA)

Single 3090 (24GB). Serialize on `~/.quant-gpu.lock`. Images: `fp-torch-gpu` (torch),
`fp-ml` (lightgbm/sklearn/scipy), `fp-gpu` (polars-GPU). Always grab the lock, run, release.

## Lock protocol
```bash
( set -o noclobber; echo "$$ $(date -u +%FT%TZ) <jobname>" > ~/.quant-gpu.lock ) 2>/dev/null \
  && trap 'rm -f ~/.quant-gpu.lock' EXIT || { echo "GPU busy:"; cat ~/.quant-gpu.lock; exit 1; }
```

## STATUS (2026-06-17)
- GPU FREE again (0% util, lock released). `fp-torch-gpu` (torch 2.3.0, CUDA verified on the 3090).
- **DONE this cycle — gpu-repr2 non-linear behavioral embedding (durable artifacts committed).**
  Branch `research/gpu-repr2-behavioral`. A multi-channel contrastive AE on 2,722 symbols × 377 daily
  bars BEAT the #76 linear c2c-SVD on held-out-time cohesion: **AE 0.131 ± 0.002 vs baseline 0.114 ±
  0.004**, AE wins all 5 seeds (min lift +0.010). NOT a rotation of #76 (ARI 0.25; canonical corrs decay
  0.95→0.31). Multi-channel *linear* PCA was WORSE (0.095) → the non-linearity, not the channels, carries
  the lift. Artifacts (under `experiments/gpu_repr2/`): `results.md`, `out/robustness_result.json`
  (load-bearing), `out/embedding_result.json`, `out/behavioral_embedding.npz`,
  **`out/behavioral_clusters_v2.parquet`** (shippable cluster map, schema == v1).
- **FEATURE OUTCOME (honest):** one additive, non-redundant outcome — the AE map is a **drop-in UPGRADE**
  to `behavioral_clusters_v1.parquet` behind the existing `peer_relative` feature (higher OOS cohesion =
  cleaner peer-demean; no degenerate tiny cluster; **zero new columns / no count bump**). Standalone AE
  coord features REJECTED as redundant/low-value (same verdict #76 reached for C2). Coordinator must gate
  the swap on **OOS-IC on real labels**, not the cohesion number alone.
- **a2a58dd2 autoencoder run: still NO durable artifact** — UNVERIFIED, treat as not-done. (MA does not
  credit claimed-but-unsaved compute. This cycle's gpu-repr2 work IS on disk and committed.)
- **Data note for this box:** `/store` is NOT mounted in this environment; the well-powered substrate used
  was the committed `certify300_daily.parquet` (2,722 syms × 377 days). Tick-store jobs below presume the
  live box where `/store/raw/{bars,trades}` exists.

## RANKED QUEUE
| # | Job | Image | Input | Output artifact (REQUIRED) | Rank rationale |
|---|-----|-------|-------|----------------------------|----------------|
| ~~R1~~ DEQUEUED | ~~R1 runner SEQUENCE model~~ | — | — | — | RESOLVED on CPU/bars (Stage-2a): give-back magnitude NOT forecastable (OOS R2 ~0); hard-fade direction mild (AUC 0.707) but redundant with F9. Tick model is n=137 → underpowered. Does NOT justify the 3090. |
| ~~#76-prod~~ SUPERSEDED | ~~Productionize the #76 stock embeddings as a parity-true feature group~~ | — | — | — | The parity-true productionized artifact is now `behavioral_clusters_v2.parquet` (a strictly better static nightly cluster map for the SAME `peer_relative` slot). The open question is no longer "can it be parity-true" (it is — static nightly lookup), but "does the better grouping pay OOS-IC on real labels". → handed to the coordinator as the v1→v2 swap + gate. |
| 1 | **Coordinator: swap `behavioral_clusters_v1`→`v2` behind `peer_relative`; run parity sweep + shuffle-canary + per-symbol-demean + OOS-IC on real labels.** Ship ONLY if OOS-IC clears the bar. | fp-ml (CPU) | `behavioral_clusters_v2.parquet` + trusted labels | parity-pass + `peer_relative_v2_oos_ic.json` | The cohesion win (+15% OOS) is necessary, not sufficient; OOS-IC on the real target is the deciding gate. No GPU needed. |
| 2 | **D3 intraday SEQUENCE world-model** (small LSTM/temporal-transformer next-state predictor over per-symbol minute sequences) → hidden-state embedding + prediction-surprise feature. Well-powered on the live box: `/store/raw/trades` 7,671 syms × 63 days. | fp-torch-gpu | `/store/raw/{bars,trades}` minute panel | `d3_sequence_result.{json,npz}` + held-out-time surprise validation | The honest next non-linear frontier #76/repr-2 don't touch: temporal DYNAMICS, not static cross-section. RT-feasible only via the FeatureState manager (stateful, parity-critical) — flag before shipping. Needs the live `/store` mount. |
| 3 | **repr-2 ablation: which channels carry the AE lift?** Re-run the AE dropping one channel at a time (overnight / intraday / logdvol / dvol_chg) → attribute the +0.017 cohesion. Cheap, sharpens the story + informs D3 inputs. | fp-torch-gpu | `out/profiles.npz` (already built) | `out/channel_ablation.json` | Fast (<2 min), no new data; turns "non-linearity helps" into "WHICH behavior the non-linearity exploits". |
| 4 | lightGBM-on-trusted (once first clean RTH day fills trusted_features) | fp-ml | trusted_features view | gbm_trusted_oos.json | waits on a clean day; CPU-ok, listed for visibility |

## Notes
- R1 GPU job RETIRED: Stage-2a (stage2_giveback_results.md) is a clean null — bars don't forecast
  give-back magnitude; the 137-tick refinement is underpowered and does NOT gate anything. GPU freed.
- No GPU job runs without committing a saved artifact path here. "Ran on the 3090" with no file = not done.
- A model on n<~200 events with walk-forward OOS is a mirage risk (RESEARCH_PITFALLS); don't burn the
  3090 on it — use lightGBM on engineered features and report the honest OOS R2 vs 0 (not vs a shuffled canary).
