# C1 v2 cluster map — OOS-IC GATE (PR #97), PRE-REGISTERED

## Decision being gated
GPUModeller's behavioral_clusters_v2 has higher UNSUPERVISED held-out COHESION (0.131 vs v1's 0.114,
+15%, 5-seed, ARI 0.25 = genuinely different partition). The coordinator will NOT swap the production
cluster map (that peer_relative reads) on cohesion alone. GATE: does v2 improve the SUPERVISED
predictive value of peer_relative — higher OOS rank-IC of the peer-demeaned return vs real forward
returns — or is the cohesion gain not translating?

## A/B (identical pipeline, only the cluster map differs)
For each map M ∈ {v1, v2}:
  peer_rel_w(symbol, minute) = ret_w(symbol, minute) − mean over symbol's M-cluster of ret_w at that minute
  (exactly the production peer_relative compute; w ∈ {5m, 30m}).
Forward return (TRADEABLE entry, per the tradeable-entry rule): fwd_h = close[entry+h]/close[entry] − 1,
where entry = the NEXT minute's close after the signal minute (never the signal minute's close).
Horizons h ∈ {5m, 30m}.

## Metric
Pooled rank-IC = Spearman(peer_rel_w(t), fwd_h(t)) over all (symbol, minute) in the TEST set.
Also the |IC| and the IC t-stat (day-clustered). Primary comparison: v2_IC vs v1_IC at matched (w,h),
same observations (same symbols/minutes — only the cluster mean subtracted differs), so it's a paired
comparison and the difference is attributable to the cluster map ALONE.

## OOS protocol
- Walk-forward by DATE: train window = the earliest 70% of dates present, TEST = the latest 30%. Report
  TEST IC only. (peer_relative has no fitted params, but the date split keeps the comparison honest and
  guards against any sample-specific fluke; the v1-vs-v2 contrast is what matters.)
- SHUFFLE CANARY: permute the cluster_id -> peer-demean against random clusters; IC must collapse toward
  0 for BOTH maps (confirms the cluster structure, not the demean mechanics, drives any IC).
- Paired: same (symbol,minute) test observations for v1 and v2 -> the IC DIFFERENCE isolates the map.

## Pre-registered verdict rule
- v2 WINS (recommend merge into tonight's deploy) iff: at the majority of (w,h) cells, v2's |IC| > v1's
  |IC| by a margin that is consistent in sign AND both beat their shuffle canary, AND v2 does not
  REGRESS materially at any cell. (A clean, consistent improvement — not a single cherry-picked cell.)
- v2 DOES NOT WIN (keep v1; GPU lift is a research note) iff the OOS-IC is flat or mixed-sign across
  cells, i.e. the cohesion gain does not translate to predictive value for peer_relative.
- Report the raw numbers either way. Honest negative is a fine outcome.

## Parity / scope
Feature-side only; no live-tree edit, no deploy. v2 is a drop-in (same schema: symbol,cluster_id,
2722 rows, 11 clusters). Study in the sandbox over real /store bars.
