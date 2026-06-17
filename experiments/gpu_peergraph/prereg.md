# Peer-GRAPH relational feature — PRE-REGISTRATION (decided BEFORE training)

## The wall this must clear (the v2-gate lesson)

The C1-v2 OOS-IC gate (`experiments/2026-06-17-c1v2-oosic-gate/results.md`) found that `peer_relative`'s
forward-IC is a REVERSAL signal whose value comes mostly from the **demean MECHANICS** (subtracting a
contemporaneous cross-sectional cluster mean ≈ market/sector neutralization), and is **nearly INSENSITIVE to
which clusters** are used: the shuffled-cluster canary nearly matched the real IC (5m/5m: real −0.0199 vs
shuffled −0.0200), and a higher-cohesion map (v2) did NOT improve — it mildly regressed — the IC. Conclusion:
"cohesion optimizes contemporaneous co-movement, not the forward-return structure of the residual."

⇒ A better hard-cluster map alone hits the SAME wall. To clear the bar, the relational structure must be used
in a genuinely NEW way that the uniform-cluster demean **cannot express**, and must demonstrably ADD
forward-IC beyond the cluster-insensitive demean.

## The genuinely-new relational feature (not a swapped cluster map)

`peer_relative` demeans against a UNIFORM mean over a HARD cluster (every cluster member weighted equally,
0/1 membership). The new feature is a **graph-WEIGHTED demean**: subtract a CONTINUOUS similarity-weighted
peer mean, where weights come from a learned per-symbol-pair graph (the GPU's relational output):

    peerrel_graph_w(i,t) = ret_w(i,t) − Σ_j W(i,j) · ret_w(j,t)   ,  Σ_j W(i,j) = 1, W(i,i)=0

W(i,j) = softmax over j of the embedding similarity (top-K neighbours, temperature τ). This is strictly
richer than the hard-cluster demean: it weights each peer by HOW similar it is and uses a per-symbol
neighbourhood (not a shared partition). The hard-cluster demean is the special case W = uniform-over-cluster.
It is parity-true: W is a FROZEN NIGHTLY per-symbol-pair static lookup (recomputed nightly from settled daily
bars, same in stream/backfill) — the `behavioral_clusters` pattern, just a weighted edge table.

## Pre-registered METRIC + BAR (forward-IC, NOT cohesion)

Metric: OOS rank-IC of `peerrel_*` vs the TRADEABLE next-minute forward return (entry = close.shift(-1)),
walk-forward TEST = latest 30% of days, paired (identical observations) — reuse the gate.py harness exactly.
Windows w ∈ {5, 30} min, forward horizons h ∈ {5, 30} min ⇒ 4 cells. (IC is NEGATIVE = reversal; "stronger"
= MORE negative |IC|.)

Three arms, paired:
  - **UNIFORM** = `peer_relative` v1 hard-cluster demean (the production baseline).
  - **GRAPH** = the graph-weighted demean above (the new relational feature).
  - **SHUFFLE canary** = the SAME graph weights with the symbol→row mapping permuted (destroys the relational
    structure, keeps the weight distribution). If GRAPH ≈ SHUFFLE, the relational weighting adds nothing.

SHIP criterion (ALL must hold), decided now:
  1. GRAPH |IC| > UNIFORM |IC| in ≥ 3 / 4 cells (pooled OOS) — it ADDS forward signal beyond the hard-cluster
     demean.
  2. GRAPH |IC| > SHUFFLE |IC| by a clear margin (the relational structure, not the demean mechanics, is what
     pays) — in ≥ 3 / 4 cells.
  3. The win is directionally consistent (GRAPH more negative, not flipping sign).

If ANY fails ⇒ **NO SHIP, decisive honest negative.** Per the team-lead's framing: if relational structure
ALSO improves only cohesion-not-IC, that tells us the GPU's value here is research/priors, and we reallocate.
A clean negative is a welcome, useful result — it closes the "is there a relational edge a simple feature
can't express" question.

## Parity note
W is a nightly per-symbol-pair frozen static lookup (sparse top-K edge table). The intraday compute is a
weighted cross-sectional reduce (Σ_j W(i,j)·ret_j over the minute cross-section) — same cross-sectional-reduce
path as `peer_relative`, parity-true by construction. No FeatureState. Only ships if it clears the bar above.
