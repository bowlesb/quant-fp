# Feature-prep overhaul — the reduced model

> **Status: DESIGN + PROOF-OF-CONCEPT (this PR).** The reduction is real and parity-safe — proven by a
> shipped POC (`DailySnapshotGroup`, two groups migrated, cell-identical). The full migration is a STAGED,
> per-pattern plan gated on Lead/Ben sign-off. Nothing in the live compute path is ripped out here.
>
> Ben's thesis (verbatim): *"the amount of code on feature prep is NOT proportional to the complexity of the
> problem. Minute bars stream in, you have a feature-group abstraction, and you need ONE running-state
> abstraction that different feature groups use in ways that suit that feature. We have too many ways to cache
> state — we need a total overhaul."* This doc answers it with numbers, names the redundant mechanisms, and
> gives the reduced design.

This doc supersedes the COST framing of `docs/STATE_ABSTRACTION.md` for the OVERHAUL question (what to delete /
unify). It is consistent with `docs/LATENCY_EXHAUSTIVE_AUDIT.md` (#381) on the per-group truth.

---

## 1. The inventory (real numbers, `quantlib/features/` on `origin/main`)

```
quantlib/features/            30,757 LOC   (88 modules, non-test)
├─ groups/ (the 64 feature groups — the actual MATH)   9,878 LOC   67 files
├─ THE ENGINE / STATE machinery (overhaul target)      ~4,743 LOC  14 files
└─ lifecycle / trust / capture / ops / backfill        ~16,000 LOC ~60 files
```

**The overhaul target is the ~4,743-LOC engine/state core**, not the 64 groups (their math is irreducible) and
not the ~16k of trust/lifecycle/capture ops (a separate surface — out of scope here). The core:

| module | LOC | role |
|---|---|---|
| `declarative.py` | 1,187 | `ReductionGroup` — pattern-B windowed-reducer engine (the big one) |
| `stateful.py` | 928 | `StatefulGroup` + `StatefulEngine` + EMA/Lag/Extrema/Cumulative state kinds |
| `incremental.py` | 844 | `WindowedSumState` + `IncrementalEngine` — the running-sum fold for B-reductions |
| `base.py` | 432 | `FeatureGroup`, `SessionCache`, `compute_latest`, `up_to_date`/`rebuild_from_history` |
| `feature_data.py` | 306 | the input-frame shapes |
| `latest.py` | 205 | `rust_reductions` / `rust_windowed_sums` — the at-T aggregate kernels |
| `reduction_anchor.py` | 199 | the centering anchors (volume/close/return) for FP conditioning |
| `registry.py` | 165 | group registration |
| `consolidated.py` | 158 | the consolidated emit driver |
| `running_state.py` | 56 | the `RunningState` Protocol (`up_to_date`/`rebuild_from_history`) |
| `session.py` + `session_cumulative.py` | 123 | session-cumulative helpers |
| `catalog.py` | 40 | catalog glue |

### How the 64 groups bind to the core today

| base class | groups | features | how state is reached |
|---|---|---|---|
| `ReductionGroup` (declarative) | 23 | ~377 | declare reduced/points/assemble → engine generates rolling (backfill) + Rust-aggregate (live) |
| `StatefulGroup` | 4 | ~87 | declare EMA/Lag/Extrema specs → `StatefulEngine` folds |
| hand-written `FeatureGroup` | 38 | ~273 | bespoke `compute()` + (often) bespoke `compute_latest()` |

The 23 + 4 = 27 declarative/stateful groups are ALREADY on a shared abstraction (good — that work is done and
parity-proven). **The redundancy Ben is pointing at is concentrated in the 38 hand-written groups and in the
NUMBER of distinct state-caching mechanisms the core exposes.**

---

## 2. The N redundant state-caching mechanisms (Ben's "too many ways to cache state")

There are **SEVEN** distinct in-process state/caching mechanisms in the core today. They are NOT seven different
problems — they are (at most) THREE patterns implemented in overlapping ways:

| # | mechanism | file | what it holds | pattern |
|---|---|---|---|---|
| 1 | **`SessionCache`** | base.py | one memoized daily-snapshot frame per group | **A** |
| 2 | bespoke `_daily_cache` / `_compute_daily` four-method dance | 9 group files | the SAME daily snapshot, hand-rolled | **A** (dup of #1) |
| 3 | **`WindowedSumState`** (+ `IncrementalEngine`) | incremental.py | running Σ/Σ²/OLS-sums per (sym,win,col) | **B** |
| 4 | **`StatefulEngine`** kinds: `EMAState`, `LastKState`, `ExtremaState`, `ReductionFoldState` | stateful.py | per-symbol EMA / last-k ring / monotonic deque / a SECOND running-sum fold | **B** |
| 5 | **`CumulativeState`** / `session_cumulative_agg` | session_cumulative.py, stateful.py | per-(sym,session) running min/max/first | **B** (cumulative sub-kind) |
| 6 | bespoke `compute_latest` short-window recompute | ~33 group files | no held state — re-slices a 30–75m window each minute | **B**/**C** (un-declared) |
| 7 | **`reduction_anchor`** centering buffers | reduction_anchor.py | per-symbol constant anchors attached pre-fold | (FP conditioning, rides B) |

**The overlaps, concretely:**

- **#1 vs #2 (the worst offender).** `SessionCache` exists precisely to kill the bespoke `_daily_cache`
  boilerplate — yet **9 Class-A groups STILL hand-write the four-method dance** (`_compute_daily` + `_daily`
  cache wrapper + `compute` broadcast + `compute_latest` latest-broadcast), and only the FIRST method differs
  between them. The cache primitive is shared; the *usage* is copy-pasted 9×. (Quantified in §4; this is the POC
  target.)
- **#3 vs #4-reduction.** `WindowedSumState` (incremental.py) and `ReductionFoldState` (stateful.py) are BOTH
  running-Σ folds over windowed columns. `ReductionFoldState` exists so a `StatefulGroup` (technical's RSI/SMA)
  can fold reductions *next to* its EMAs — but it is a second implementation of the same additive-window kind
  the `ReductionGroup` tier already owns. One additive-window primitive should serve both tiers.
- **#5 vs #3.** `CumulativeState` (running min/max/first, no expiry) is the **degenerate, infinite-window** case
  of the additive/extrema kinds — a separate class for "the window is the whole session."
- **#6 is un-declared B/C.** ~33 groups override `compute_latest` to slice a bounded window and recompute
  (`compute_latest_on_window`, or a bespoke pass). This is correct and bounded, but it is a THIRD way to express
  "this feature only needs the last W minutes" — neither a declared reduction (#3) nor a declared stateful kind
  (#4). It's a per-group hand-roll of what the kind taxonomy should declare.

**Net:** seven mechanisms, three patterns. The held-state contract (`RunningState`: `up_to_date` /
`rebuild_from_history`) already unifies the *staleness/reseed* concern across all of them (base.py:349-375,
running_state.py) — that abstraction is good and stays. What is NOT unified is **which state kinds exist and how
a group declares its pattern**: that is fragmented across #1-#6.

---

## 3. The reduced model — A / B / C + ONE running-state primitive

Every one of the 64 groups is provably one of three patterns. Mapped mechanically (`isinstance` +
`reduce_buffer_minutes()` + `compute_latest`/`SessionCache` overrides), corroborated by #381:

### Pattern A — intraday-invariant → compute once + cache (13 groups / 99 features)
The value for `(symbol, date)` is a pure function of the per-session-CONSTANT daily snapshot — identical at
every minute. **State = one memoized frame per session.** Compute once, broadcast.
> `multi_day`, `multi_day_vwap`, `prior_day`, `daily_beta`, `liquidity_rank`, `overnight_beta`,
> `overnight_intraday_split`, `return_dispersion` (hybrid), `breadth`, + the 5 pure-ts reference filters
> (`sector`, `calendar`, `asset_flags`, `round_levels`, `calendar_events`).

### Pattern B — windowed reducer → prior state + O(1) per-minute fold (27 groups / ~464 features)
The value is a reduction over a trailing window (sum / mean / std / OLS / EMA / extrema / cumulative).
**State = a running accumulator per (symbol, window, column).** Fold the new minute, expire the old one.
> The 23 `ReductionGroup` + 4 `StatefulGroup` already here. PLUS the ~33 hand-written groups whose bespoke
> `compute_latest` is really a bounded-window reduction not yet *declared* as one (the §2 #6 bucket).

### Pattern C — point-in-time / event (the residual hand-written groups)
Last-event, minutes-since, a small fixed lookback. **State = a tiny ring of the last k minutes** (already the
`LastKState` kind) or no state at all (pure at-T). Most are already cheap single-minute passes.

### The ONE primitive: `RunningState` (already on `base.FeatureGroup`)
A / B / C differ only in *what* state they hold; they share *how* staleness and reseed work. That is the single
held-state contract, already landed:

```python
if not group.up_to_date(buffer):     # cold / session-boundary / gap / hot-swap / rewind
    group.rebuild_from_history(buffer)  # one-time reseed from the SAME history backfill uses
# ... fold the unabsorbed tail, emit (O(1)/minute) ...
```

**A is the degenerate case** (`up_to_date` = "is the snapshot witness unchanged?", `rebuild` = recompute the
snapshot). **B is the additive/EMA/extrema/cumulative folds.** **C is the last-k ring (or stateless).** One
guard, one reseed, parity-by-construction for all three (`rebuild_from_history` seeds from the same window
backfill recomputes over → live state == backfill state the instant `up_to_date` flips true).

So the target is: **ONE `RunningState` primitive + a small closed set of state KINDS (additive-window, EMA,
last-k, extrema, cumulative, daily-snapshot), each implemented and parity-tested ONCE in the engine. A group
declares its pattern + kind; it never hand-rolls the fast/backfill split or the cache.** The kinds for B are
already built (`WindowedSumState`/`EMAState`/`LastKState`/`ExtremaState`). The gap is (a) A is not yet a
declared base — it's the copy-pasted dance; (b) #6's bounded-window groups aren't declared as kinds; (c) the
two running-sum folds (#3, #4-reduction) and the cumulative class (#5) should collapse to one additive kind.

---

## 4. What collapses into what (the deletion / consolidation plan)

| consolidation | from | to | LOC reclaimed (est.) |
|---|---|---|---|
| **A1** Class-A daily-snapshot dance → `DailySnapshotGroup` base | 9 groups × 4-method boilerplate | 1 base (POC, ~100 LOC) + 9 groups writing 1 method each | ~250–350 |
| **A2** the 5 pure-ts reference filters → trivially A (declare-only) | bespoke `compute()`+`compute_latest()` | a 1-method `daily_snapshot` (or a `ReferenceGroup` sibling) | ~80 |
| **B1** `ReductionFoldState` (stateful.py) → reuse `WindowedSumState` | the second running-sum fold | the one additive-window primitive | ~120 |
| **B2** `CumulativeState`/#5 → infinite-window additive/extrema kind | a separate class + `session_cumulative_agg` | a `window=session` flag on the existing kinds | ~100 |
| **B3** #6 bounded-window `compute_latest` overrides → declared reduction kind | ~33 bespoke `compute_latest` | `reduce_buffer_minutes()` + declared windows; engine generates the slice | ~400–600 (incremental) |
| **C1** point-in-time groups → declared last-k / at-T | bespoke `compute_latest` | `LastKState` kind or no state | (subset of B3) |

**Realistic reclaim of the ~4,743-LOC core + the group-level boilerplate: on the order of 1,000–1,500 LOC of
mechanism deleted, and — more importantly — SEVEN state mechanisms reduced to ONE primitive + ~6 declared
kinds.** The win is debuggability (one place each kind lives, one parity invariant per kind), not just line
count. Ben's bar — *"code proportional to the small problem"* — is met when a new feature group is "declare your
pattern + write your math," never "re-implement the live/backfill split or pick a cache."

**What is explicitly NOT deleted / NOT touched:**
- The 64 groups' actual math (irreducible — that IS the problem's complexity).
- `RunningState` / `up_to_date` / `rebuild_from_history` (the good unifying contract — it STAYS, everything
  routes through it).
- The trust / lifecycle / capture / backfill ~16k LOC (a separate surface; not this overhaul).
- The NO-GO-8 reductions' batch path (a settled float-cancellation limit, not a redundancy).

---

## 5. The migration path (staged, parity-preserving, fp-neutral)

This CANNOT change any feature value — it is a refactor. Every stage is gated on the existing generic parity
test (`tests/test_fp_latest.py`: `compute_latest == compute().filter(T)` for every group) PLUS a per-stage
migrated-vs-origin cell-identity test, and the registry surface-hash (group/feature names + versions) must stay
byte-identical (fp unchanged).

- **Stage 0 — POC (THIS PR).** `DailySnapshotGroup` base + migrate `multi_day` + `multi_day_vwap`. Proven
  cell-identical to origin/main; fp unchanged (64 groups / 737 features, surface-hash unchanged). This proves
  the reduction is real and safe on the highest-frequency redundancy (#1-vs-#2).
- **Stage 1 — finish Pattern A (A1+A2).** Migrate the remaining 7 daily-snapshot groups + the 5 reference
  filters onto the base. Pure mechanical, each gated by its cell-identity test. (~330–430 LOC out.)
- **Stage 2 — collapse the B duplicates (B1+B2).** Point `ReductionFoldState` at `WindowedSumState`; fold
  `CumulativeState` into an infinite-window kind. Gated by `test_fp_stateful` + `test_fp_incremental` parity.
  (~220 LOC out.) Independent of the FP_INCREMENTAL live flip (that's an activation lever, not this refactor).
- **Stage 3 — declare the bounded-window groups (B3/C1).** Convert the ~33 bespoke `compute_latest` overrides
  to declared windows so the engine generates the slice. The riskiest stage (touches the most groups) → do it
  group-by-group, each behind the generic latest-parity test. The largest line reclaim.

Each stage is independently shippable, parity-gated, and fp-neutral. None requires a coordinated fc+strategy
deploy (no fingerprint change) — they deploy on the normal feature-computer relaunch.

---

## 6. POC evidence (this PR)

- **New:** `quantlib/features/daily_snapshot_group.py` (`DailySnapshotGroup`, ~100 LOC) — owns the
  cache + broadcast + live/backfill split for Pattern A. A group implements ONLY `daily_snapshot(source)`.
- **Migrated:** `multi_day.py` (120 → 87 LOC), `multi_day_vwap.py` (91 → 68 LOC) — 4 methods → 1.
- **Proof:** `tests/test_daily_snapshot_group.py` — migrated `compute()` (10,000 rows) and `compute_latest()`
  (40 rows) are `.equals()` cell-identical to the origin/main four-method versions on the standard frames; the
  generic `test_fp_latest` passes for both; the registry surface-hash is unchanged (`937c0065b9a1`, 64/737).
- **Suite:** `test_fp_latest.py` + `test_fp_new_families.py` + the new POC test = 111 passed, 1 skipped.

The pattern generalizes directly to the other 7 daily-snapshot groups (identical four-method shape, confirmed)
— Stage 1 is mechanical from here.
