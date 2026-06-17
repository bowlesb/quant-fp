# Peer-GRAPH relational feature (results): DECISIVE NULL — and the GPU-feature hunt is exhausted

## The test (pre-registered, v2-gate-aware)

The C1-v2 gate found `peer_relative`'s forward-IC is mostly the demean MECHANICS, nearly insensitive to which
clusters (shuffle ≈ real). So a better cluster map = same wall. This lane tested a genuinely NEW relational
feature the uniform demean cannot express: a **graph-WEIGHTED demean** —
`peerrel_graph(i) = ret_i − Σ_j W(i,j)·ret_j`, W = softmax over top-K embedding-similar peers (continuous,
per-symbol, weighted by similarity). Pre-registered bar (`prereg.md`): GRAPH |IC| must beat UNIFORM (v1
cluster demean) AND its SHUFFLE canary in ≥3/4 cells on TRADEABLE forward returns; else decisive negative.

## Result — DECISIVE NULL (well-powered: 9.6M rows, 18 OOS days)

| ret/fwd | uniform IC | graph IC | shuffle IC | graph beats uniform? |
|---|---|---|---|---|
| 5m / 5m | **−0.01986** | −0.01503 | −0.01567 | NO |
| 5m / 30m | **−0.00992** | −0.00721 | −0.00680 | NO |
| 30m / 5m | **−0.00853** | −0.00571 | −0.00511 | NO |
| 30m / 30m | **−0.00672** | −0.00378 | −0.00136 | NO |

**Graph loses to uniform in ALL 4/4 cells.** (IC is negative = reversal; "stronger" = more negative. Uniform
is more negative everywhere.) Graph beats its own shuffle in 3/4 — so the embedding DOES capture real
structure — but concentrating the demean on similar peers makes the reversal signal WEAKER, not stronger,
than flat broad neutralization. `ship_graph_feature: false`.

**The deepest read of the v2-gate insight:** `peer_relative`'s forward value IS the demean MECHANICS — flat
cross-sectional neutralization — full stop. Weighting the neutralization toward behaviorally-similar peers
(hard clusters OR soft graph weights) REMOVES forward signal vs neutralizing against the broad set. The edge
is neutralization BREADTH, not relational structure. Relational similarity, however well learned, is the
wrong thing to concentrate the demean on.

## SYNTHESIS across all 5 GPU cycles — the GPU-embedding-as-feature hunt is EXHAUSTED

| cycle | question | result |
|---|---|---|
| **repr-2** | better behavioral embedding → better cluster map | cohesion ↑ +15% OOS, but forward-IC FLAT (v2 gate declined the swap) |
| **day-ahead** | per-symbol embedding → next-day target | next-day vol predictable (IC ~0.32) but ALREADY captured by `daily_vol_*d`; embedding redundant |
| **D3** | single-name minute world-model | NULL — only vol clustering (redundant) |
| **lead-lag** | cross-sectional minute next-move | NULL — IC 0.0003, overfits |
| **peer-graph** | similarity-weighted relational demean | NULL — loses to uniform 4/4 |

**Conclusion:** across direct prediction (minute single-name, minute cross-sectional, daily) AND relational
representation (clusters, graph), **no GPU embedding beats simple features on forward-IC.** The wells are:
- minute-return prediction — null both ways (D3, lead-lag);
- daily prediction — real but simple-feature territory (day-ahead vol = `daily_vol_*d`);
- relational structure — real structure, but it does NOT add forward-IC over flat neutralization (repr-2 v2,
  peer-graph).

There is no shippable GPU FEATURE in this space. The priors are banked and genuinely valuable (they tell the
platform where NOT to look, and confirm existing simple features already capture the predictable structure).

## STRATEGIC PIVOT (per the Lead): stop mining; the GPU's value is the MODEL

The 3090's real near-term payoff is the eventual MODEL — a lightGBM/NN trained on the full TRUSTED feature set
once the first trusted cohort grades (imminent). That is where the GPU earns its keep, not in synthesizing yet
another embedding feature. **Speculative feature-embedding exploration is PAUSED.** Stand by to build the
model the moment a trusted feature set lands. (If a genuinely different GPU direction with a real prior
appears — not prediction, not relational demean — propose it; otherwise rest.)

## Artifacts (`experiments/gpu_peergraph/`)

| file | what |
|---|---|
| `prereg.md` | the pre-registered bar (forward-IC, not cohesion; shuffle canary) |
| `build_graph.py` | graph-embedding → sparse weighted edge table W (parity-true nightly lookup) |
| `gate.py` | graph vs uniform vs shuffle forward-IC gate (tradeable entry, paired) |
| `out/gate_result.json` | **the verdict** — graph loses to uniform 4/4 |
| `out/graph_weights.parquet`, `out/graph_embedding.npz` | the graph (provenance) |
