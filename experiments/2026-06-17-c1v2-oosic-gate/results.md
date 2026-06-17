# C1 v2 cluster map — OOS-IC GATE RESULTS (PR #97)

Gate: does behavioral_clusters_v2 (OOS cohesion 0.131 vs v1's 0.114, +15%) improve the SUPERVISED
predictive value of `peer_relative` — or is the cohesion gain not translating? Paired A/B: same
(symbol, minute) observations, only the subtracted cluster mean differs. Sample: top-1000 liquid by
adv$ (956 with data), 60 recent days; walk-forward TEST = latest 30% (2.89M rows, 18 days). Forward
returns booked from the TRADEABLE next-minute close (per the tradeable-entry rule).

## OOS rank-IC vs forward returns (TEST set, paired)
| signal | fwd | v1 IC | v2 IC | v2−v1 (|IC|) | shuffle IC | days v2>v1 | winner |
|---|---|---|---|---|---|---|---|
| peerrel_5m | 5m | **−0.01986** | −0.01976 | −0.00009 | −0.01996 | 11/18 (61%) | **v1** |
| peerrel_5m | 30m | **−0.00992** | −0.00786 | −0.00206 | −0.00898 | 9/18 (50%) | **v1** |
| peerrel_30m | 5m | **−0.00853** | −0.00679 | −0.00174 | −0.00666 | 11/18 (61%) | **v1** |
| peerrel_30m | 30m | **−0.00672** | −0.00320 | −0.00353 | −0.00235 | 12/18 (67%) | **v1** |

(The IC is NEGATIVE — the peer-relative recent return REVERSES into the forward return, a reversal
signal. "Better" = a STRONGER, i.e. more-negative, IC. v1 is more negative in EVERY cell.)

## Verdict — v2 does NOT win. KEEP v1.
- **v1 wins all 4/4 cells on pooled OOS-IC.** v2 does not just fail to improve the peer-demean — it
  MILDLY REGRESSES it: under v2 the demeaned residual carries LESS forward-predictive (reversal)
  signal than under v1, at every (return-window, forward-horizon) pair. The gap WIDENS at the longer
  cells (peerrel_30m/30m: v1 −0.0067 vs v2 −0.0032, roughly half the signal).
- **The per-day robustness is mixed (50–67%), not a clean v2 edge** — and the pooled IC, the metric
  that matters, favors v1 everywhere.
- **Canary read:** the shuffled-cluster IC sits CLOSE to the real IC in most cells (e.g. 5m/5m: real
  −0.0199 vs shuffled −0.0200) — i.e. most of peer_relative's forward-predictive value comes from the
  demean MECHANICS (subtracting a contemporaneous cross-sectional mean ~ a market/sector neutralization)
  rather than from WHICH behavioral clusters are used. Where the cluster choice DOES move the IC, v1's
  partition is the better neutralizer and v2 is barely above random. This explains why a better
  UNSUPERVISED cohesion (tighter within-cluster co-movement) need not improve the SUPERVISED forward-IC:
  cohesion optimizes contemporaneous co-movement, not the forward-return structure of the residual.

## Recommendation
- **Do NOT merge #97 into tonight's deploy.** Keep the production v1 cluster map; `peer_relative` is
  unchanged. The v2 contrastive-AE embedding is a real, OOS-validated COHESION improvement and a
  legitimate research result (a genuinely different, arguably cleaner partition, ARI 0.25), but the
  cohesion gain does NOT translate to peer_relative's predictive value — so it is a research note, not
  a production swap. GPUModeller's #97 stands as the embedding artifact; the swap is declined on the
  supervised gate.
- Honest negative. The gate did its job: it prevented a cohesion-driven swap that would have mildly
  WEAKENED the live feature.

## Caveat / scope
Single liquid sample, 60-day window, 18 OOS days — directional and paired, so the v1>v2 contrast is
robust to the sample (both arms see identical observations), but the absolute IC magnitudes are
window-specific. The DIRECTION (v1 ≥ v2 in every cell, widening with horizon) is the load-bearing
result and it is consistent across all four cells + the longer-horizon cells most strongly.
