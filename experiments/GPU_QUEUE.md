# GPU job QUEUE — owned by Modelling Agent (MA)

Single 3090 (24GB). Serialize on `~/.quant-gpu.lock`. Images: `fp-torch-gpu` (torch),
`fp-ml` (lightgbm/sklearn/scipy), `fp-gpu` (polars-GPU). Always grab the lock, run, release.

## Lock protocol
```bash
( set -o noclobber; echo "$$ $(date -u +%FT%TZ) <jobname>" > ~/.quant-gpu.lock ) 2>/dev/null \
  && trap 'rm -f ~/.quant-gpu.lock' EXIT || { echo "GPU busy:"; cat ~/.quant-gpu.lock; exit 1; }
```

## STATUS (2026-06-17)
- GPU FREE (0% util, lock released). `fp-torch-gpu` (torch 2.3.0, CUDA verified). `fp_store_real` volume
  mounts read-only in the GPU container (`-v fp_store_real:/store:ro`): `/store/raw/{bars,trades,quotes}`.
  bars = 7,682 symbols × ~379 days minute OHLCV; trades = 7,671 symbols.
- **repr-2 (PR #97, branch research/gpu-repr2-behavioral) — DONE.** Non-linear contrastive AE behavioral
  embedding BEAT the #76 linear c2c-SVD on held-out cohesion (0.131±0.002 vs 0.114±0.004, wins all 5 seeds,
  non-redundant ARI 0.25). Ships `behavioral_clusters_v2.parquet` as a drop-in upgrade behind `peer_relative`
  (zero new columns). HELD by Lead for the OOS-IC gate before the v1→v2 swap. Artifacts:
  `experiments/gpu_repr2/`.
- **D3 intraday SEQUENCE world-model — DONE, HONEST NULL (no feature).** LSTM next-state predictor on 104k
  RTH minute sequences (top-300 × 379 days). Beats persistence ~50% OOS — BUT the predict-zero diagnostic
  shows it's volatility clustering, not return prediction (logret skill vs zero = +0.77%; range = +46%).
  Surprise feature would re-encode realized vol → REDUNDANT, and it's STATEFUL (heavy FeatureState build).
  Do NOT ship. Useful prior: minute returns are not forecastable from price/volume path alone. Artifacts:
  `experiments/gpu_repr2_d3/` (`worldmodel_result.json`, `diagnose_result.json` load-bearing, `results.md`).
- **a2a58dd2 autoencoder run: still NO durable artifact** — UNVERIFIED, not-done. (MA does not credit
  claimed-but-unsaved compute.)

## RANKED QUEUE
| # | Job | Image | Input | Output artifact (REQUIRED) | Rank rationale |
|---|-----|-------|-------|----------------------------|----------------|
| ~~R1~~ DEQUEUED | ~~R1 runner SEQUENCE model~~ | — | — | — | RESOLVED (Stage-2a null). |
| ~~D3~~ DONE | ~~intraday LSTM world-model~~ | — | — | `experiments/gpu_repr2_d3/diagnose_result.json` | NULL: edge is vol clustering (redundant), returns unpredictable. Honest no-ship. |
| 1 | **Coordinator: swap `behavioral_clusters_v1`→`v2` behind `peer_relative`; OOS-IC gate** | fp-ml (CPU) | `behavioral_clusters_v2.parquet` + labels | `peer_relative_v2_oos_ic.json` | repr-2 cohesion win is necessary not sufficient; OOS-IC decides. No GPU. |
| 2 | **CROSS-SECTIONAL intraday lead-lag / flow model** (the honest successor to D3's null): does symbol A's minute move predict symbol B's NEXT minute move, cross-sectionally? A graph/attention model over the live minute cross-section. NOT single-name autoregression (D3 proved that's a vol-only null). | fp-torch-gpu | `/store/raw/bars` minute cross-section | `leadlag_result.{json,npz}` + OOS held-out-time IC vs a contemporaneous-corr baseline | D3 showed single-name dynamics = vol clustering. The unexploited frontier is CROSS-symbol next-move structure (lead-lag), which simple per-symbol features can't see. Well-powered (7,682 syms). |
| 3 | **repr-2 channel ablation** (drop overnight/intraday/logdvol/dvol_chg one at a time) → attribute the AE's +0.017 cohesion lift. | fp-torch-gpu | `experiments/gpu_repr2/out/profiles.npz` | `channel_ablation.json` | Cheap (<2 min), sharpens the repr-2 story, no new data. |
| 4 | lightGBM-on-trusted (once first clean RTH day fills trusted_features) | fp-ml | trusted_features view | gbm_trusted_oos.json | waits on a clean day; CPU-ok, visibility. |

## Notes
- No GPU job runs without committing a saved artifact path here. "Ran on the 3090" with no file = not done.
- A model on n<~200 events with walk-forward OOS is a mirage risk (RESEARCH_PITFALLS); use the well-powered
  cross-asset data (thousands of symbols), not underpowered single-event tasks.
- **predict-zero, not just persistence:** for z-scored / mean-0 targets, persistence is a weak baseline that
  inflates apparent skill (the D3 trap — "50% vs persistence" was +0.77% vs predict-zero). Always include the
  mean baseline and decompose per-channel before claiming a feature.
- **GPU container store mount:** `docker run --gpus all -v fp_store_real:/store:ro -v <worktree>:/work -w /work
  fp-torch-gpu ...`. The `/store` host path is NOT mounted; use the docker volume.
- **OOM lesson:** mini-batch BOTH train AND eval; keep the full panel on CPU and move batches to the 24 GB
  GPU per-step. A full-tensor eval forward on 100k×390 sequences tries to alloc >100 GB.
