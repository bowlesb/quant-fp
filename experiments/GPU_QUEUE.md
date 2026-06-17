# GPU job QUEUE — owned by Modelling Agent (MA)

Single 3090 (24GB). Serialize on `~/.quant-gpu.lock`. Images: `fp-torch-gpu` (torch),
`fp-ml` (lightgbm/sklearn/scipy), `fp-gpu` (polars-GPU). Always grab the lock, run, release.

## Lock protocol
```bash
( set -o noclobber; echo "$$ $(date -u +%FT%TZ) <jobname>" > ~/.quant-gpu.lock ) 2>/dev/null \
  && trap 'rm -f ~/.quant-gpu.lock' EXIT || { echo "GPU busy:"; cat ~/.quant-gpu.lock; exit 1; }
```

## STATUS (2026-06-16)
- GPU FREE (0% util, lock absent). `fp-torch-gpu` built ~14 min ago by embeddings agent a2a58dd2.
- **a2a58dd2 autoencoder run: NO durable artifact found** in experiments/ or /tmp. Result UNVERIFIED
  — nothing to review. Treat as not-done until an artifact lands. (MA does not credit claimed-but-
  unsaved compute.)

## RANKED QUEUE
| # | Job | Image | Input | Output artifact (REQUIRED) | Rank rationale |
|---|-----|-------|-------|----------------------------|----------------|
| 1 | R1 small-cap-runner SEQUENCE model — predict intraday continuation vs fade from first-30-min tick/bar sequence | fp-torch-gpu | runner_events.parquet + selective tick backfill | runner_seq_oos.json (walk-fwd OOS AUC, shuffle-canary) | highest-priority lane; runners sidestep friction wall; ML on sequence is the natural GPU job |
| 2 | Re-run / verify the autoencoder embedding on the minute-bar panel WITH a saved artifact (latent vectors + recon loss + a feature-candidate writeup) | fp-torch-gpu | /store minute panel | autoenc_latent.parquet + autoenc_report.md | the embeddings agent's job, but it must produce a durable artifact to count |
| 3 | lightGBM-on-trusted (once first clean RTH day fills trusted_features) | fp-ml | trusted_features view | gbm_trusted_oos.json | waits on a clean day; CPU-ok but listed for visibility |

## Notes
- Job 1 (runner sequence) depends on R1 Stage-1 bars characterization (running now) → then selective
  tick backfill of the runner-day symbols only (cheap; ~hundreds of symbol-days) → then GPU sequence model.
- No GPU job runs without committing a saved artifact path here. "Ran on the 3090" with no file = not done.
