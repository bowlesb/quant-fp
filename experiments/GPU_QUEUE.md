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
- **DATA-COVERAGE UNBLOCK for job 1:** `/store/raw/trades` covers **7,671 symbols × ~63 days
  (2026-03-18 → 2026-06-16)**; `/store/raw/bars` covers **7,682 symbols × ~379 days
  (2024-12-11 → 2026-06-17)**. ⇒ NO separate selective tick-backfill needed for any runner-day
  inside the 63-day trade window — ticks already present. Job 1 reads ticks directly. Runner-days
  OLDER than 2026-03-18 are bars-only (sequence model restricted to the 63-day tick window).
- **a2a58dd2 autoencoder run: NO durable artifact found** in experiments/ or /tmp. Result UNVERIFIED
  — nothing to review. Treat as not-done until an artifact lands. (MA does not credit claimed-but-
  unsaved compute.)

## RANKED QUEUE
| # | Job | Image | Input | Output artifact (REQUIRED) | Rank rationale |
|---|-----|-------|-------|----------------------------|----------------|
| ~~1~~ DEQUEUED | ~~R1 runner SEQUENCE model~~ | — | — | — | RESOLVED on CPU/bars (Stage-2a): give-back magnitude NOT forecastable (OOS R2 ~0); hard-fade direction mild (AUC 0.707) but redundant with F9. Tick model is n=137 → underpowered, low-priority exploratory only. Does NOT justify the 3090. |
| 1 | Productionize the #76 stock embeddings as a PARITY-TRUE feature group (real-time computable per-minute, compute==compute_latest) — IF the embeddings are point-in-time and a deterministic function of the live panel | fp-torch-gpu | merged #76 embedding harness | embedding_group + test_fp_latest pass + feature-candidate writeup | #76 is merged (OOS-validated embeddings); the open question is whether they can be made parity-true live. Coordinate with the embeddings agent / Lead. |
| 2 | lightGBM-on-trusted (once first clean RTH day fills trusted_features) | fp-ml | trusted_features view | gbm_trusted_oos.json | waits on a clean day; CPU-ok but listed for visibility |

## Notes
- R1 GPU job RETIRED: Stage-2a (stage2_giveback_results.md) is a clean null — bars don't forecast
  give-back magnitude; the 137-tick refinement is underpowered and does NOT gate anything. GPU freed.
- No GPU job runs without committing a saved artifact path here. "Ran on the 3090" with no file = not done.
- A model on n<~200 events with walk-forward OOS is a mirage risk (RESEARCH_PITFALLS); don't burn the
  3090 on it — use lightGBM on engineered features and report the honest OOS R2 vs 0 (not vs a shuffled canary).
