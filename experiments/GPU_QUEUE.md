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
- **lead-lag cross-sectional model — DONE, HONEST NULL (no feature).** LSTM over the minute (resid,flow,market)
  cross-section → next-minute residual return, top-300 × 379 days. Held-out cross-sectional IC = **0.0003**;
  the model OVERFITS (held-out MSE ~17× worse than predict-zero). No cross-symbol next-minute structure.
  Artifacts: `experiments/gpu_leadlag/` (`leadlag_result.json`, `results.md`).
- **STRATEGIC PRIOR (two converging nulls):** MINUTE-LEVEL return prediction from the price/volume path is a
  NULL **both ways** — single-name AR (D3) and cross-sectional lead-lag. Stop mining direct minute-return
  forecasting. The GPU's demonstrated edge is **REPRESENTATION** (repr-2's behavioral embedding: coherent
  clusters, +15% OOS cohesion → a parity-true static feature, the `peer_relative` pattern). Re-rank toward
  representation + DIFFERENT targets than minute returns (queue below).
- **a2a58dd2 autoencoder run: still NO durable artifact** — UNVERIFIED, not-done.

## RANKED QUEUE (re-ranked toward representation; minute-return prediction retired as a dry well)
| # | Job | Image | Input | Output artifact (REQUIRED) | Rank rationale |
|---|-----|-------|-------|----------------------------|----------------|
| ~~R1 / D3 / lead-lag~~ DONE | minute-return prediction (3 variants) | — | — | their `*_result.json` | ALL NULL. Minute returns not forecastable from price/volume path (single-name OR cross-sectional). Retired. |
| 1 | **Coordinator: swap `behavioral_clusters_v1`→`v2` behind `peer_relative`; OOS-IC gate** | fp-ml (CPU) | `behavioral_clusters_v2.parquet` + labels | `peer_relative_v2_oos_ic.json` | repr-2 cohesion win is necessary not sufficient; OOS-IC decides. No GPU. |
| 2 | **DAY-AHEAD behavioral-state embedding → next-DAY (not minute) target.** Extend repr-2: a per-symbol embedding of the recent multi-day behavioral path; probe whether it carries OOS structure for a DAILY-horizon target (next-day residual return / realized-vol / overnight-gap), via cross-sectional IC. | fp-torch-gpu | daily panel + `out/profiles.npz` | `dayahead_repr_result.json` + held-out-time IC vs predict-zero | Pivot per the prior: representation (repr-2's proven edge) on a DIFFERENT, slower target where structure plausibly survives (daily vol/peer effects are real OOS; minute returns are not). |
| 3 | **repr-2 channel ablation** (drop overnight/intraday/logdvol/dvol_chg one at a time) → attribute the AE's +0.017 cohesion lift; also test ADDING channels (realized-vol, gap, turnover-trend) to push cohesion. | fp-torch-gpu | `experiments/gpu_repr2/out/profiles.npz` | `channel_ablation.json` | Cheap (<2 min), sharpens + potentially strengthens the one real GPU win. |
| 4 | **Sector/peer-graph embedding** (GNN/contrastive over the co-movement graph) → an even-better cluster map than v2, same `peer_relative` slot. | fp-torch-gpu | daily co-movement matrix | `graph_embed_result.json` + held-out cohesion vs v2 | Doubles down on representation; ships into the existing parity-true slot (zero new columns), like v2. |
| 5 | lightGBM-on-trusted (once first clean RTH day fills trusted_features) | fp-ml | trusted_features view | gbm_trusted_oos.json | waits on a clean day; CPU-ok, visibility. |

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
- **Overfit-vs-zero is a null tell:** when a high-capacity model's held-out MSE is WORSE than predict-zero
  (lead-lag: 17× worse), there is no signal — it memorized noise. Don't chase it; bank the prior.
- **MA workflow fix:** when a dataset build finishes, LAUNCH the training in the SAME tool round — do NOT
  pause between (the D3 + lead-lag idle-after-build stalls let the GPU sit idle ~1h with data ready).
