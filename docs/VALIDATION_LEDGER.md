# Validation Ledger — per-datapoint real-time-vs-backfill trust

> Status: DESIGN (2026-06-14). The operational "real thing" that supersedes the dev-time
> `compute_latest()==compute()` unit check. Owner's framing: *"every single feature value collected
> in real-time should be validated later via an equivalent backfill operation, and we attach a
> validated status directly to the feature/feature group. Move toward a state where we are sure X% of
> our real-time collected data is trustable — which tells us whether we can trust a feature at all if
> we train on backfill and run it in production."*

## 1. Why this and not the parity test

`parity.py` recomputes both sides fresh and prints a per-(feature,tier) pass/fail **score** for one
day. That is a development check: it proves the *code* agrees given inputs, and it is ephemeral —
nothing is stored, nothing accumulates, nothing tells you on a Tuesday in August "is `volume_zscore_30m`
trustworthy enough to train on?"

The ledger is different in three ways:

1. **Per-datapoint, on STORED values.** We compare the values we *actually wrote live*
   (`source=stream`, from `compute_latest` during capture) against the values *backfill produced*
   (`source=backfill`, from `compute` during materialize) — cell for cell, keyed `(feature, symbol,
   minute)`. This catches input divergence AND live-vs-backfill compute drift AND coverage gaps, on
   real data, the way the prior system never did.
2. **Durable + accumulating.** Every validation run writes a record. Trust is the *accumulation* of
   these records over days, not a single green run.
3. **Attached to the feature.** A feature carries a `trust_grade` and `status` derived from its
   validation history. Training reads can *require* a trust floor, so a model can never train on a
   feature we have not proven reproduces live.

## 2. The grain problem and how we solve it

A literal per-cell status flag is 519 features × 10k symbols × 390 minutes ≈ **2 billion cells/day** —
we will not store 2B status rows. Instead we store the status *derivably and compactly*, in three
layers, so the status of ANY cell is recoverable by query:

```
cell_status(feature, symbol, minute, day) =
    mismatch    if (f,s,m) in validation_exception
    missing     if rollup says s had missing-live cells AND (f,s,m) absent from stream  (derivable)
    match       if the (day,f,s) rollup exists and the cell isn't an exception
    uncompared  if no rollup row for (day,f,s)   (day not validated, or symbol not in scope)
```

Mismatches and "extra-live" cells (live present, backfill null) are RARE by design, so we store them
explicitly. Matches and coverage-gaps are the bulk, so we store their *counts*, not rows.

## 3. Schema

### Layer 1 — `validation_exception` (parquet, partitioned by `date=`)
One row per cell that did NOT cleanly match. Expected to be small.

| col | type | meaning |
|-----|------|---------|
| feature | str | feature name |
| symbol | str | ticker |
| minute | datetime(us, UTC) | the cell |
| status | enum | `mismatch` \| `extra_live` (live had a value, backfill null) |
| stream_value | f64 | what we collected live |
| backfill_value | f64 \| null | what backfill produced |
| abs_err | f64 | \|stream − backfill\| |
| rel_err | f64 | abs_err / max(\|backfill\|, floor) |

We do NOT row-per-cell the `missing_live` case (stream null / backfill present) — at a 20% coverage
gap that is ~400M rows. It is captured as a count in Layer 2 and as the morning warmup is expected.

### Layer 2 — `validation_cell` (parquet, partitioned by `date=`)
Per `(day, feature, symbol)` rollup — ~5M rows/day, the per-feature/per-symbol detail.

| col | type | meaning |
|-----|------|---------|
| feature, symbol | str | |
| tier | i32 | liquidity tier on that day (from `load_tiers`) |
| n_compared | i32 | cells where both sides non-null |
| n_match | i32 | within tolerance |
| n_mismatch | i32 | value disagreement (→ exceptions) |
| n_extra_live | i32 | live non-null, backfill null (→ exceptions) |
| n_missing_live | i32 | live null, backfill non-null (coverage gap; warmup) |
| match_rate | f64 | n_match / n_compared (null if n_compared==0) |
| worst_abs_err, worst_rel_err | f64 | the largest divergence seen |

### Layer 3 — `feature_trust` (small table — one row per `(feature, version)`)
THE status attached to the feature. Updated by folding each validated day in.

| col | type | meaning |
|-----|------|---------|
| feature, version | str | |
| status | enum | `unvalidated` \| `validating` \| `certified` \| `divergent` |
| trust_grade | enum | `A` ≥0.9999 · `B` ≥0.999 · `C` ≥0.99 · `F` below · `U` unvalidated |
| n_days_validated | i32 | distinct days folded in |
| lifetime_compared | i64 | Σ n_compared |
| lifetime_match | i64 | Σ n_match |
| lifetime_match_rate | f64 | lifetime_match / lifetime_compared |
| last_validated_day | date | most recent fold |
| last_day_match_rate | f64 | that day's rate (regression signal) |

`status`: `validating` until `n_days_validated ≥ MIN_DAYS` (e.g. 5); then `certified` if
`trust_grade ≥ B`, else `divergent`. A single day below a hard floor flips `divergent` immediately
(loud — a feature that broke must not silently stay certified).

## 4. The validation job (`quantlib/features/validate.py`)

```
validate(root, day, scope="universe"):
  1. ensure backfill exists for `day` (else raise / trigger materialize) — backfill is truth.
  2. stream   = store.get_features(all_features, scope, day, source="stream")
     backfill = store.get_features(all_features, scope, day, source="backfill")
  3. for each feature column: classify every (symbol, minute) cell
       both non-null   -> match if within tol(feature) else mismatch
       live-only       -> extra_live
       backfill-only   -> missing_live
     tolerance reuses the feature's declared parity tolerance (base.py spec).
  4. write Layer-1 exceptions (mismatch + extra_live) and Layer-2 rollup for `day`.
  5. fold the day into Layer-3 feature_trust (idempotent on re-run for the same day:
     subtract the prior fold for `day` if present, then add — so re-validating a day is safe).
  6. exit non-zero if ANY feature's day match_rate < its hard floor (loud, gates a certify wrapper).
```

Idempotency: re-running `validate(day)` overwrites that day's Layer-1/2 partitions and re-folds
Layer-3 by day, so it is safe to re-run after a backfill correction.

## 5. How this earns "backfill further back with confidence"

The chain the owner wants:

1. **Monday**: capture live → `source=stream`.
2. **Tuesday (T+1)**: backfill Monday → `source=backfill`; run `validate(Monday)`. This writes the
   ledger and updates trust. We now KNOW, per feature, what % of Monday's live cells reproduced.
3. **Certify (code-parity only)**: a feature reaching `trust_grade ≥ B` over `MIN_DAYS` is
   `certified`. This proves ONE thing precisely — *the live `compute_latest` path reproduces the
   settled `compute` path on the recent overlap dates, on real data*. It does **NOT** by itself prove
   that backfill of a date we never captured live equals what live *would have* collected then.
   Certification is code-parity, not deep-history correctness. (Earlier drafts of this doc claimed
   "trustworthy by transitivity" — that conflated the two and was wrong.)
4. **Deep-history trust (a STRICTER, separate status)**: a feature is `deep_trust` only when, on top of
   `certified`, the deep-history *inputs* are proven equal to what live would have seen — which
   requires the audit P0s to be closed: point-in-time reference/universe (`load_reference`/`load_tiers`
   historized), corporate-action adjustment wired, and a stable historical tape. Until those land,
   `certified` features may be extended backward ONLY for inputs that are genuinely time-invariant
   (pure intraday tape math); reference/multi-day/split-touching features are NOT deep-trustworthy and
   are flagged in the training export. The ledger is the gate either way: uncertified/divergent
   features are never extended.

This is also the train/serve-gap measurement: a model trains on `source=backfill`; in production it
sees `source=stream`. The feature's `lifetime_match_rate` IS the train/serve fidelity of that input.
A feature with grade F is a feature whose backfilled training values do not match what production will
feed it — a latent model failure we now surface *before* deployment.

## 6. Edge cases the ledger must handle (from the parity audit)

- **Warmup / buffer-edge** (audit P0-2): the first ~`window` minutes of a session are legitimately
  `missing_live` for long-window features (live buffer too short). These must count as `missing_live`,
  NOT `mismatch`, and must be excluded from the match-rate denominator — otherwise every morning tanks
  the grade. The ledger separates `n_missing_live` from `n_mismatch` for exactly this reason.
- **Reference/universe historization** (audit P0-1): deep-history backfill must use point-in-time
  sector/tier, or the backfill leg is itself wrong and the "transitivity" in §5.3 is invalid. The
  ledger CANNOT certify deep history until reference is historized — tracked separately.
- **Splits** (audit P1): a split inside a validated window will surface as a wall of `mismatch` on the
  multi-day features for that symbol — the ledger is the *detector* for the unwired corporate-actions
  guard, and those mismatches must block certification of the affected feature-days.
- **Coverage gap ~20%** (audit P1): if it is `missing_live` it lands in `n_missing_live` (visible, not
  fatal). If it manifests as `mismatch`, the ledger flags it loudly. Either way it is now measured per
  feature instead of a single hand-waved day-level number.

## 7. Build order

1. Schema + writers/readers (`validation_store.py`) + Layer-3 fold — with unit tests on synthetic
   stream/backfill frames (match / mismatch / extra / missing / warmup).
2. `validate.py` job wiring the real store + a CLI (`python -m quantlib.features.validate <day>`).
3. `feature_trust` read API + a `require_trusted` floor on `store.get_features` (training gate).
4. Backfill the audit P0s (reference historization, buffer assertion) so deep certification is sound.

## 8. Review resolutions (binding — override the body above where they differ)

A critical review (2026-06-14) grounded against `compare.py`/`store.py`/`base.py` produced these
binding decisions:

1. **One shared cell-match predicate.** Extract `cell_verdict(spec, live_col, back_col) -> Expr`
   into `compare.py`; BOTH `diff()` and `validate.py` call it, so the ledger can never drift from the
   parity code. It dispatches on `spec.parity_method` and storage class:
   - **tolerance features**: `match ⇔ |live−back| ≤ 1e-12 + tol·|back|` (the exact existing predicate,
     `compare.py:116`; pure-relative — do NOT add a large abs floor that masks small-scale features).
   - **flag / UInt8 / small-int features**: `match ⇔ live == back` (exact; a flag is right or wrong).
   - **distributional features** (`parity_method="distributional"`): cell-for-cell is meaningless by
     design — validate at **(day, feature, tier) grain** via the paired `dist_score`, NOT per-cell.
     These produce NO Layer-1 exception rows (else the "exceptions are rare" assumption explodes) and a
     `dist_pass` boolean in a parallel `validation_dist` rollup.
   Layer-1 `rel_err` uses the SAME denominator as the predicate (`|back|`, `1e-12` floor).

2. **Two grades, not one** (Layer-3). `value_grade = match/compared` (excludes warmup) AND
   `coverage_grade = compared / (compared + missing_live − warmup_allowance)`, where the per-feature
   warmup allowance is derived from `nan_policy="warmup"` + the feature window. A feature certifies
   only when BOTH clear their floors — otherwise a feature that silently stops collecting half its
   cells gets an A on value while being broken. Re-imports the `COVERAGE_FLOOR=0.95` the raw ledger
   would otherwise drop.

3. **Layer-3 = durable lifetime sums + a `folded_days` guard; fold once per settled day.** Keep i64
   `lifetime_compared`/`lifetime_match` (no float drift; derive the rate). Do NOT subtract-then-add.
   Record each folded day in a `folded_days` set so a dropped-and-rematerialized day can't double-fold.
   `feature_trust` is **retention-exempt** (tiny). Layer-1/2 ledger partitions live UNDER a dedicated
   `validation/` root so `store.drop_before`'s `date=` glob never silently eats them; they follow their
   own retention.

4. **Validate only SETTLED days.** `validate.py` asserts the day is closed (not today / capture
   finished) before reading `source=stream`, mirroring `require_settled` — else a partial live day
   reads as a wall of false `missing_live`. Validate **per group** against `settled_dates()`: a group
   without backfill for the day is reported `unvalidated`, not a global raise. Features in stored
   history but dropped from the current registry are skipped with a logged note (not a `_resolve` raise).

5. **Cell-compare is a long-form unpivot + one `group_by([feature,symbol])` per group** — NOT the
   per-(feature,tier) `.item()` loop in `diff()` (unusable at ~2e9 cells/day). Benchmark this step
   before wiring the full universe.

6. **Per-cell status needs the ledger PLUS the retained stream/backfill partitions** (the ledger alone
   stores counts + explicit mismatches, not every missing minute). §2's predicate is ordered
   `mismatch → missing → match` (missing tested before match). This is honest and sufficient.

7. **Trust is per `(feature, version)`.** A version bump resets to `unvalidated` (new code re-earns
   trust); the `require_trusted` training gate checks the EXACT version being read, not the latest.

### Build order (revised)
1. `compare.cell_verdict` (shared predicate, the 3 dispatch branches) + unit tests.
2. `validation_store.py` — Layer-1/2/dist schemas, writers/readers under a `validation/` root.
3. `validate.py` — settled-day guard, per-group compare via unpivot+group_by, writes Layer-1/2,
   folds Layer-3 with `folded_days`; CLI `python -m quantlib.features.validate <day>`; exit non-zero
   on any feature-day below its hard floor.
4. `feature_trust` read API + `require_trusted` floor on `store.get_features` (the training gate).
5. THEN the audit P0s (reference historization, buffer assertion, corporate-action wiring) to unlock
   `deep_trust`.
