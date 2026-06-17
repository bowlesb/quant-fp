# Cross-sectional intraday LEAD-LAG model (results): an HONEST NULL

**Lane:** financial world-views — cross-symbol temporal structure. **GPU:** RTX 3090 via `fp-torch-gpu`.
**Substrate (well-powered):** the `fp_store_real` bars volume → a per-day aligned **(minute × symbol)**
cross-section, top-300 liquid universe × ~379 days (~298/300 symbols present each minute — dense).

## Question

D3 proved single-name minute-return autoregression is a vol-only null. This asks the orthogonal question:
does the minute-`t` CROSS-SECTION (other symbols' returns + signed flow) predict symbol `i`'s minute-`(t+1)`
**market-residualized** return — i.e. is there cross-symbol LEAD-LAG structure beyond contemporaneous beta?
Target is residualized against the equal-weight cross-sectional mean each minute, so we are NOT rediscovering
"everything co-moves with the market" (that's contemporaneous, not predictive).

## Model + pre-registered bar

LSTM over the cross-sectional state `x_t = [resid_t (S), flow_t (S), market_t]` (2S+1 dims) → predicts
`resid_{t+1}` (S dims). Pre-registered (the D3 baseline lesson applied — proper baselines, not just
persistence): held-out TIME (last 20% of dates); beat **predict-zero** AND **own-lag (AR1)** on next-minute
residual MSE; require a positive held-out **cross-sectional IC**. Else honest null.

## Result — a decisive NULL

| held-out time (next-minute residual) | value |
|---|---|
| **cross-sectional IC** (corr predicted vs realized resid_{t+1}) | **0.0003** |
| LSTM MSE | 3.52e-05 |
| predict-zero MSE | 2.0e-06 |
| own-lag (AR1) MSE | 4.2e-06 |
| LSTM beats predict-zero? | **NO** (−1623% — it overfits) |
| ship feature? | **NO** |

Two honest readings, both pointing the same way:
1. **IC ≈ 0.0003** — there is essentially no cross-sectional predictive structure in next-minute residual
   returns. A's minute move does not predict B's next-minute move (after removing the contemporaneous market).
2. **The LSTM OVERFITS badly** — held-out MSE is ~17× WORSE than just predicting zero. With no real signal,
   the high-capacity model memorized train noise and generalized worse than the trivial floor. Residual
   minute returns are so close to mean-zero that **predict-zero is nearly unbeatable** — the honest sign of
   an efficient, unpredictable target at this resolution.

**VERDICT: do NOT ship a lead-lag feature. Clean null, bank the prior.** The pre-registration + the
predict-zero/own-lag baselines did their job again (as in D3): they stopped a high-capacity model's
in-sample fit from being mistaken for an edge.

## Strategic synthesis (the well is dry for minute-return prediction)

Two GPU cycles now converge on one prior:

| direction | result |
|---|---|
| D3 — single-name minute AR | NULL (only vol clustering, redundant) |
| lead-lag — cross-sectional minute structure | NULL (IC 0.0003, overfits) |

**MINUTE-LEVEL return prediction from the price/volume path is a null BOTH ways** — single-name and
cross-sectional. This is consistent with the scoreboard ("simple-signal edges are arbitraged away") and is a
genuinely useful platform prior: stop mining direct minute-return forecasting from price/volume.

Where the GPU HAS paid: **representation** (repr-2's behavioral embedding — economically-coherent clusters,
+15% OOS cohesion, a real parity-true static feature). The GPU's edge is learning STRUCTURE/embeddings that
become parity-true static features (the `peer_relative` pattern), not direct minute-return regression. The
re-ranked queue (in `GPU_QUEUE.md`) pivots accordingly: richer/longer-horizon REPRESENTATIONS and
DIFFERENT targets than minute returns — not more minute-return prediction.

## Artifacts (committed under `experiments/gpu_leadlag/`)

| file | what |
|---|---|
| `build_crosssection.py` | per-day (minute × symbol) residual-return + flow cross-section builder |
| `train_leadlag.py` | cross-sectional LSTM + predict-zero / own-lag baselines + cross-sectional IC |
| `out/leadlag_result.json` | **the verdict** — held-out IC 0.0003, ship=false |
| `out/leadlag_lstm.pt` | trained weights (provenance) |

(`out/crosssection.npz` — 229 MB — is gitignored; regenerable from `build_crosssection.py` + the bars store.)
