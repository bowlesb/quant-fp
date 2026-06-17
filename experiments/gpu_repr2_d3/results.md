# D3 — intraday SEQUENCE world-model (results): an HONEST NULL for a novel feature

**Lane:** financial world-views — temporal DYNAMICS (orthogonal to the static cross-sectional embeddings of
#76 / repr-2). **GPU:** RTX 3090 via `fp-torch-gpu`. **Substrate (well-powered):** `fp_store_real` bars
volume — **104,176 RTH minute sequences** (top-300 liquid universe × ~379 days, 2024-12-11 → 2026-06-16),
390 minutes/session × 6 per-minute behavioral features.

## What I ran

A one-layer LSTM next-state predictor: given a symbol's RTH minute path so far, predict the next minute's
(logret, range) — the two scale-free behavior channels (each z-scored within session). The hidden state is a
learned dynamics embedding; the per-minute prediction surprise (‖pred − actual‖²) is the candidate feature.
Pre-registered bar (PLAN.md §D3): beat a baseline on held-out SYMBOLS and held-out TIME, and the surprise
must be non-redundant with realized range.

## Headline result (looks great) — then the honesty diagnostic (kills it)

**vs PERSISTENCE baseline (predict next = last):** LSTM beats it by ~50% on next-minute MSE, on held-out
time (49.9%) AND held-out symbols (50.0%) — generalizes cleanly. `worldmodel_result.json`.

**But persistence is a WEAK baseline for z-scored (mean-0) returns.** The honesty diagnostic
(`diagnose_result.json`) adds a PREDICT-ZERO baseline and a per-channel decomposition on held-out time:

| next-minute MSE (held-out time) | logret channel | range channel |
|---|---|---|
| persistence (predict last) | 2.062 | 0.859 |
| **predict-zero** | **1.003** | 0.873 |
| LSTM | 0.995 | **0.470** |

- **LSTM vs predict-zero, logret channel: +0.77%.** Next-minute return is **essentially unpredictable** —
  the LSTM adds nothing over "predict 0" on direction/magnitude (as market efficiency predicts at the minute
  scale). The 50% "win" vs persistence was just persistence chasing noise.
- **LSTM vs predict-zero, range channel: +46%.** This is real, but it is **volatility CLUSTERING** — range
  is strongly autocorrelated (high-vol minutes follow high-vol minutes). The LSTM learned "volatility
  persists."
- `corr(surprise, current_range) = 0.295`, `corr(surprise, next_range) = 0.66`: the surprise scalar is a
  volatility-tracking quantity.

## Honest feature verdict: DO NOT SHIP a D3 feature (a clean null, like Stage-2a)

The entire learnable signal is volatility clustering in the range channel — which **existing realized-range /
volatility features already capture**. The "prediction surprise" feature would mostly re-encode realized
volatility (it is high exactly when range jumps), not a novel "unusual-dynamics" signal. And the genuinely
novel part a world-model would need — **forecasting next-minute returns** — is a null (+0.77% over zero).

So per the pre-registered rule, this is a **NULL for a novel, non-redundant feature**. We do NOT promote a
D3 feature off this run. The pre-registration + the predict-zero diagnostic did their job: they stopped a
flattering-but-redundant "50% skill" number from becoming a shipped feature that just re-encodes vol.

This is also a useful POSITIVE finding for the platform's priors: **minute-level intraday returns are not
forecastable from price/volume path alone at this resolution** (consistent with the scoreboard's
"simple-signal edges are arbitraged away"). The only learnable intraday structure here is volatility
clustering, already covered.

### Cost/benefit note (why not chase it further)
Pushing this (bigger model, attention, more channels) would, at best, predict volatility slightly better —
still a vol feature, still redundant. The parity cost is also high: the surprise is a STATEFUL feature
(rolling per-symbol LSTM hidden state) that would have to live in the FeatureState manager (state_spec /
seed / fold / emit) to keep backfill == stream — a heavy, parity-critical build to ship a redundant vol
proxy. Not worth it. If a future lane wants intraday dynamics, the honest target is **cross-sectional**
next-move structure (lead-lag / flow), not single-name autoregression — queued below.

## Artifacts (committed under `experiments/gpu_repr2_d3/`)

| file | what |
|---|---|
| `build_sequences.py` | RTH minute-sequence builder from `/store/raw/bars` (Int8-overflow bug fixed) |
| `train_worldmodel.py` | LSTM next-state trainer (mini-batched; fits 24 GB) |
| `diagnose.py` | the load-bearing honesty diagnostic (predict-zero + per-channel) |
| `out/worldmodel_result.json` | headline vs-persistence skill (held-out time + symbols) |
| `out/diagnose_result.json` | **the verdict** — predict-zero baseline + per-channel decomposition |
| `out/worldmodel_lstm.pt` | trained LSTM weights (provenance) |

(`out/sequences.npz` — the 104k-session dataset — is 722 MB and intentionally NOT committed; it is
regenerable from `build_sequences.py` + the bars store. The scripts + result JSONs are the durable record.)
