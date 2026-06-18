# Feature Trust — Redesign (binary, reproducible, self-checking)

Status: design of record. Supersedes the 4-state lifecycle (PENDING/VALIDATED/DIVERGENT/NULL)
in `docs/TRUST_METADATA.md` / `docs/PARITY_LIFECYCLE.md`. Authored 2026-06-17 per Ben's direction.

## The principle

We never spend compute on a feature's history unless we can TRUST it — meaning its real-time
(stream) value reproduces its backfill recomputation. Trust is earned, then exploited (backfill +
model). The old process made earning trust too slow (a 2-clean-day gate, scarce clean days) and the
state model too elaborate to reason about. This redesign keeps the rigor, drops the ceremony, and
makes every trust decision **reproducible from the stored record** and **continuously re-checked**.

## What changes (the five decisions)

1. **Binary state.** A `(feature, version)` is `TRUSTED` or `NON_TRUSTED`. Nothing else. The old
   PENDING/VALIDATED/DIVERGENT/NULL collapse into these two. "Why not trusted / what failed" is NOT
   a state — it lives in the append-only check history (`feature_trust_check`) for triage.

2. **Trust is keyed by `(feature, version)` and is PERMANENT.** Once `(feature, vX)` is TRUSTED it
   stays TRUSTED forever — it is an immutable historical fact ("vX of this feature reproduced backfill
   on day D"). A code change **bumps the version**; the new `(feature, vX+1)` row starts NON_TRUSTED
   and earns trust on its own. We never "demote on code change" — there is nothing to demote, because
   the old version's trust was about the old code. The ONLY thing that flips TRUSTED→NON_TRUSTED is an
   explicit random-check failure (below), which is a deliberate, logged operator-grade action.

3. **One clean day earns trust.** A `(feature, version)` becomes TRUSTED when, on **one clean
   regular-session day**, its stream value matched its backfill recomputation within the feature's
   tolerance on at least `min_pass_rate` of compared cells. (Was two days.) The safety this gives up —
   a one-day fluke — is bought back, more strongly, by continuous random re-checks. If a feature ever
   slips through and a random check catches it, we investigate and, if warranted, tighten that
   feature kind to two days. Default fast, escalate on evidence.

4. **Deterministic features are TRUSTED by construction.** Calendar / time-of-day / static-lookup
   features are pure functions of the timestamp (and static maps) with no real-time dependency, so
   stream == backfill is guaranteed. They are auto-trusted with `reason='deterministic'` and need no
   parity day. (Closing the gap that calendar features sat ungraded.)

5. **Every trust decision is reproducible, and re-checked.** When we trust, we record the *provenance*
   that lets us replay the verdict: the day, the feature version, the **git commit**, and a
   **content hash of the group's compute code**. A scheduled **random-check** job periodically replays
   parity for random TRUSTED features on random recent clean days; a threshold violation un-trusts the
   feature, files a defect, and we investigate — keep it (if the check was unsound) or deprecate/delete
   it (if the divergence is real).

## Tolerance policy (per feature type)

"Match within tolerance" is `|stream - backfill| <= 1e-12 + rtol * |backfill|` per cell (the existing
`FeatureSpec.tolerance` relative-tolerance rule). The redesign sets a **default rtol + min_pass_rate
per `FeatureType`**, overridable per feature via `FeatureSpec.tolerance`. Rationale: exactly-reproducible
features (returns, flags) must match near-perfectly; windowed/float-order-sensitive features (rolling
stats, technicals) carry legitimate summation-order noise and need a looser band; deterministic
features must match exactly.

| Class | FeatureTypes | rtol | min_pass_rate | notes |
|-------|--------------|------|---------------|-------|
| Deterministic | CALENDAR | exact | n/a | trusted by construction, no parity day (pure timestamp) |
| Exact numeric | PRICE, VOLUME, PRICE_VOLUME, CANDLESTICK, REFERENCE | 1e-6 | 0.9999 | algebraic from bars / static maps; REFERENCE earns one trivial clean day (its reference frame could differ stream↔backfill, so we don't blind-trust it) |
| Windowed | TECHNICAL, VOLATILITY, MOMENTUM, TREND_QUALITY, MULTI_DAY | 1e-4 | 0.999 | rolling/order-sensitive |
| Flow | TRADE_FLOW, MICROSTRUCTURE, QUOTE_SPREAD | 1e-4 | 0.999 | tick-aggregation sensitive |
| Cross-sectional | CROSS_SECTIONAL | 1e-4 | 0.999 | MUST validate over full universe |

Per-feature override wins over the type default (a feature can declare a tighter/looser
`tolerance`). The policy is data, in `quantlib/features/trust_policy.py`, unit-tested, and surfaced so
the threshold behind any trust grant is auditable.

## Provenance & reproducibility

The trust grant records, per `(feature, version)`:

- `trusted_day` — the clean day that earned it
- `version` — the feature group version trusted
- `trusted_git_commit` — repo commit at trust time
- `trusted_content_hash` — blake2b of the group's compute source (`inspect.getsource`)
- `trust_value_rate`, `trust_tolerance`, `trust_min_pass_rate` — the numbers behind the grant

This makes trust a replayable function: given the stored stream + raw tape + the code at
`trusted_git_commit`/`trusted_content_hash`, recomputing backfill reproduces the same verdict. The
content hash closes the "edited the compute but forgot to bump the version" hole — if the live code's
hash differs from a trusted row's hash at the same version, that is a discipline violation the
random-check surfaces (the grant was for different code).

## Random checks (the safety net that buys 1-day trust)

`quantlib/features/trust_random_check.py`, run on a cron (registered in `docs/OPERATIONS.md`):

1. Sample N random TRUSTED `(feature, version)` rows.
2. For each, pick a random recent **clean** day with both stream and backfill present (cross-sectional
   features force a full-universe materialize — a bounded sample falsely fails breadth/rank).
3. Recompute backfill, compare to stored stream within the feature's tolerance.
4. **Pass** → append a passing `feature_trust_check` row (reaffirmation).
   **Fail on a clean day** → un-trust (`trust_state='NON_TRUSTED'`, `reason='random_check_failed'`),
   append the failing check row, file a `feature_parity_defect`. Then a human/agent investigates:
   - the check was unsound (contaminated day slipped the gate, too few cells) → re-affirm, keep;
   - the divergence is real → deprecate/delete the feature, or fix + re-earn at a new version.

Contamination-awareness is preserved: we only un-trust on a **clean-day** failure (reusing the
gather-coherence / cleanliness gates), so a capture glitch never condemns a correct feature.

## State + tables

- `feature_trust` (existing, `(feature,version)` PK) gains the binary `trust_state` and the provenance
  columns. The old `status` / `lifecycle_state` / grade columns are retained as **diagnostics** (not the
  gate) and will be dropped once nothing reads them.
- `feature_trust_check` (new, append-only) — one row per check (initial grant + every random check):
  `(feature, version, check_kind, checked_day, content_hash, git_commit, value_rate, tolerance,
  min_pass_rate, n_compared, passed, action, checked_at)`. The reproducible audit trail.
- `feature_validation_day`, `feature_validation_exception`, `feature_parity_defect` — unchanged
  (per-day cell rollup, diverging-cell evidence, defect backlog).
- `trusted_features` / `feature_trust_summary` views — re-pointed to `trust_state`.

Downstream (backfill gate, training export, strategies) gates on `trust_state='TRUSTED'` — one
predicate, replacing the `lifecycle_state='VALIDATED'` check.

## Migration

Existing `lifecycle_state='VALIDATED'` rows → `trust_state='TRUSTED'` (reason `legacy_validated`); all
else → `NON_TRUSTED`. No trust is lost. Deterministic groups are auto-trusted on the first grading run
after deploy.

## Edge cases (decided)

- **Trust-then-backfill is speculative.** Trusting on one day then deep-backfilling means a later
  random-check failure invalidates that work → un-trust ALSO quarantines the feature's backfilled
  history (keep + flag, never delete), per the parity-trust-lifecycle.
- **Version/code drift** is handled by versioning, not demotion (decision 2) + the content-hash check.
- **No drift/warning bookkeeping.** We don't track trends. A check either holds the threshold or it
  doesn't; failure → un-trust + investigate. Simpler, and the investigation is where judgment lives.
- **Cross-sectional features** must be validated/checked over the full universe, never a sample.
