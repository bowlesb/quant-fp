# Feature Candidates from the Representation-Learning Lane

Each candidate states: source embedding, definition, **real-time feasibility**, parity story, and the gate
it must still pass (shuffle-canary / per-symbol-demean / OOS-IC) before the coordinator ships it. The lane
does NOT touch the live tree; promotion is a FeatureGroup PR the MA/coordinator runs through parity/canary/trust.

## Ranked by ship-readiness

### C1 — Behavioral-peer-relative return  (from D4 stock embeddings) ★ MOST READY
- **Source:** the SVD stock embedding on real daily returns (clusters persist OOS: within−across corr 0.092
  held-out vs 0.0003 random — 300x; see `out/stock_embeddings_result.json`).
- **Definition (per symbol-minute):** `peer_rel_ret = ret_today(symbol) − mean(ret_today over its
  behavioral-peer cluster)`. Relative strength vs the data-driven peer group (not GICS — the *behavioral*
  group, which is what actually co-moves).
- **Real-time feasibility:** YES, trivially.
  - The embedding + cluster assignment is a **static per-symbol lookup** recomputed NIGHTLY from settled
    daily bars (slow-moving; a symbol's behavioral cluster does not change intraday). Refresh = a batch job
    that writes a `symbol -> cluster_id` table; identical in stream and backfill -> **parity-true by
    construction** (same lookup table, no intraday state).
  - The intraday compute is `ret − peer_mean_ret`: a cheap aggregate over already-computed per-symbol
    returns. Sub-ms.
- **Parity story:** the only non-trivial part is the peer-mean — it needs the cross-section's returns at
  minute t. The platform already computes cross-sectional/breadth aggregates, so this rides the same
  cross-sectional-reduce path; backfill replays the same minute cross-section -> parity holds.
- **Still must pass:** shuffle-canary (permute labels -> IC ~ 0), per-symbol-demean (does it survive
  removing symbol fixed effects? — relative-return is already demeaned-ish, good sign), OOS-IC on real
  labels once the vector backfill + labels land.

### C2 — Static stock-embedding coordinates  (from D4)
- **Definition:** the top-`k` (e.g. 8) embedding dims per symbol, as slow per-symbol features.
- **RT feasibility:** YES (static nightly lookup, like C1). Parity-trivial.
- **Caveat:** these are near-constant within a day -> low cross-sectional time-variation; likely act as a
  learned sector/style control rather than an alpha. Useful as CONDITIONING features (interactions) more
  than standalone. Ship behind C1.

### C3 — VAE reconstruction error  (from D1)  [pending VAE result]
- **Definition (per symbol-minute):** `recon_err = ||x − decode(encode(x))||` over the 519-vector — an
  "unusual market state" anomaly scalar (states the model finds surprising).
- **RT feasibility:** YES if the VAE clears its generalization bar (held-out symbol/time recon R2). Inference
  = one numpy feedforward over the live 519-vector (encoder weights exported to `.npz`, no torch). Sub-ms,
  deterministic, stateless -> parity-true.
- **Gate:** must FIRST pass the pre-registered D1 checks (else it's memorizing, and recon_err is meaningless).
  Then the standard feature gate.

### C4 — VAE latent dims  (from D1)  [pending VAE result]
- **Definition:** the `z` latent coords as a compressed market-state embedding (16 features replacing 519).
- **RT feasibility:** YES (same feedforward). Value: dimensionality reduction / denoised state for downstream
  models, and regime conditioning (feeds D5).
- **Gate:** only ship if VAE beats PCA on the pre-registered bar (else ship PCA components — simpler,
  parity-trivial, no learned weights to version).

### C5 — Regime id + soft membership  (from D5, builds on D1)  [future]
- **Definition:** cluster-assign the current market-aggregate state -> regime id one-hot + soft weights.
  A market-context feature shared across symbols; directly feeds regime-conditional strategies (W11 pays in
  high-dispersion regimes — this would let strategies gate on a learned regime label).
- **RT feasibility:** YES (assign current market vector — deterministic). Highest strategic relevance.

## Promotion checklist (for the coordinator, per candidate)
1. Export model -> `.npz` (numpy-only inference; no torch in the live image).
2. Implement deterministic `FeatureGroup` in `quantlib/features/groups/`; for static lookups (C1/C2) add
   the nightly refresh job; for stateful dynamics (D3) use the FeatureState manager (backfill==stream).
3. Nightly parity sweep + shuffle-canary + per-symbol-demean + OOS-IC.
4. Register trust grade; bump feature-set version (CLAUDE.md feature-count assertion).
5. Coordinator runs the coordinated deploy. **This lane never edits the live tree.**
</content>
