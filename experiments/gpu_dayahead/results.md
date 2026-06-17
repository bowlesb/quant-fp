# Day-ahead behavioral embedding (results): embedding REDUNDANT, but a simple vol feature IS the deliverable

**Lane:** representation (the GPU's proven edge) on a SLOWER target than minute returns (which D3 + lead-lag
proved is a null). **GPU:** RTX 3090 via `fp-torch-gpu`. **Substrate (very well-powered):** 907,027
(symbol, day) rows, **3,802 symbols**, 2025-03-13 → 2026-06-16, built parity-true (trailing-window profile as
of day T → day-(T+1) target; held-out-time split by date).

## Design (parity-true by construction, per the brief)

Per (symbol, day T): a 12-dim TRAILING behavioral profile using only data ≤ T (trailing 60-day return/vol/
volume/gap stats — backward-looking, no look-ahead). Targets at T+1: `resid_ret_next` (market-residualized
return), `realized_vol_next` (|next-day return|), `overnight_gap_next` (|next-day overnight|). In production
this is a FROZEN NIGHTLY per-symbol static lookup (the `behavioral_clusters` pattern) — no FeatureState.

The validation goes straight to the FEATURE bar (the v2-gate lesson): held-out-time cross-sectional IC, and
the learned embedding must BEAT the best SIMPLE trailing feature to be non-redundant.

## Result — honest, held-out-time cross-sectional IC

| target | best SIMPLE feature (IC) | learned embedding (IC) | embedding beats simple? |
|---|---|---|---|
| `resid_ret_next` | `c2c_mean` (−0.045) | 0.029 | NO |
| `realized_vol_next` | **`intraday_std` (0.324)** | 0.317 | NO |
| `overnight_gap_next` | **`intraday_std` (0.278)** | 0.271 | NO |

Two clear findings:

1. **Next-day realized vol / gap IS strongly predictable** — held-out IC ~**0.32** from a simple trailing
   volatility feature. This is REAL, persistent structure (daily volatility clustering), exactly the
   "slower target where structure survives OOS" the pivot predicted. Day-ahead RETURN is a null (best IC
   −0.045), as expected — daily returns are hard.

2. **The learned embedding is REDUNDANT.** On every target it ties-or-loses to the single best trailing
   feature (vol: 0.317 vs 0.324; gap: 0.271 vs 0.278). The AE just re-encodes the trailing-vol information it
   was built from; the non-linearity / 8-dim embedding adds nothing over `trailing intraday_std`.

## Honest verdict

**DO NOT ship a learned day-ahead EMBEDDING feature** — it does not beat the trivial trailing-vol feature
(the v2-gate bar, applied to myself: structure that doesn't beat the simple baseline is not a shippable
feature). The GPU/representation does not earn its complexity on this target.

**BUT the lane produced a genuinely useful deliverable (no GPU needed):** next-day realized-vol / gap has a
strong, simple, parity-true predictor — **trailing realized volatility (`intraday_std` / `c2c_std`,
held-out IC ~0.32)**. If the platform does not already have a daily trailing-realized-vol feature, *that
simple feature* is worth a (CPU, parity-trivial) feature PR — it's a frozen nightly per-symbol static lookup,
the cheapest possible parity story, and it predicts a real target. **Recommend the coordinator check the
existing feature set for a trailing-daily-vol feature; ship the simple one if absent. The GPU embedding is
not the deliverable — the simple feature is.** (Queued in `GPU_QUEUE.md`.)

## Strategic update (three GPU cycles, one consistent shape)

| cycle | finding |
|---|---|
| D3 / lead-lag | minute-return prediction = null both ways (retired) |
| repr-2 | representation WINS for static peer STRUCTURE (cohesion), shipped v2 cluster map |
| day-ahead (this) | the predictable day-ahead target (vol) is captured by a SIMPLE feature; the learned embedding adds nothing |

Refined prior: **the GPU's representation edge is for STRUCTURE/relationships (peer/co-movement clusters)
that simple per-symbol features can't express — NOT for predicting per-symbol scalar targets a single trailing
statistic already captures.** Direct-prediction targets (returns AND vol) are either null or simple-feature
territory; the embedding's value is relational (peer_relative). Future GPU jobs should target RELATIONAL
structure, not scalar prediction.

## Artifacts (`experiments/gpu_dayahead/`)

| file | what |
|---|---|
| `build_dayahead.py` | parity-true trailing-profile + next-day-target builder (no look-ahead) |
| `train_dayahead.py` | AE embedding + simple-baseline-vs-embedding held-out IC (the verdict) |
| `out/dayahead_result.json` | **the verdict** — per-target simple vs learned held-out IC |
| `out/dayahead_embedding.npz`, `out/dayahead_ae.pt` | embedding + weights (provenance) |

(`out/dayahead.npz` — 51 MB — gitignored; regenerable from `build_dayahead.py` + the daily panel.)
