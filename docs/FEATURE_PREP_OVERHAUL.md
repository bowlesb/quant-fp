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
- **#3 vs #4-reduction — CORRECTION (verified 2026-06-24): NOT a duplicate.** `ReductionFoldState` (stateful.py)
  does NOT reimplement `WindowedSumState` — it **HOLDS** one (`self.state = WindowedSumState(...)`) and wraps it
  with the per-symbol time-based prior-close + value-row construction a `StatefulGroup` needs to fold reductions
  next to its EMAs. It is a thin composition over the one additive primitive, not a second fold. **No B1
  consolidation to do.** (The earlier "second running-sum fold" framing was wrong — the code already shares the
  primitive.)
- **#5 vs #3 — CORRECTION (verified 2026-06-24): a justified distinct kind, not redundancy.** `CumulativeState`
  is the session-cumulative reduce (min/max/sum/first) with a **session-rollover reset** — folding a
  `WindowedSumState` with `window=session` would NOT capture min/max/first or the per-session reset, and it is
  already O(symbols)/minute (the minimum). The 3-group live redundancy it could have had (3× tz-conversion + 3×
  session-filter + 3× group_by) was ALREADY consolidated into ONE shared memoized pass by
  `session_cumulative_agg` (#266/#267/#269/#284). **No B2 consolidation to do.**
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

**Stage-1 finding — Pattern A is NOT one uniform shape; it has THREE sub-shapes** (surfaced by migrating the
groups, exactly the taxonomy validation the POC was for):
- **A.1 — pure daily-snapshot broadcast** (`DailySnapshotGroup` fits directly): the finished per-(symbol,date)
  features are joined onto every minute, no per-minute math. → `multi_day`, `multi_day_vwap`, `daily_beta`,
  `overnight_beta`, `overnight_intraday_split`, `liquidity_rank` (the last with an extra universe witness).
  **All 6 migrated (Stage 0 + Stage 1).**
- **A.2 — snapshot LEVELS + at-T per-minute expr**: the snapshot holds per-(symbol,date) LEVELS, then the final
  feature mixes a level with the at-T `close` per minute (`close/prev_high − 1`). → `prior_day`. Needs a tiny
  optional `broadcast_exprs` hook on the base (a real sub-pattern, not a fork). **Deferred to Stage 1b.**
- **A.3 — A-cache + B-gather HYBRID (NOT pure A)**: a per-minute cross-sectional GATHER (group_by minute →
  market/sector scalar over the whole universe) whose daily inputs are A-cached but whose reduce is a live
  Pattern-B operation. → `return_dispersion`, `breadth`. **These do NOT belong on `DailySnapshotGroup`** — their
  `SessionCache` use is already correct (the A part); the gather is a legitimate B. **Left as-is.** (Corrects
  the earlier "13 Pattern-A groups" framing: 11 are A-shaped, 2 are A+B hybrids whose A-cache is already clean.)
> A.1+A.2: `multi_day`, `multi_day_vwap`, `prior_day`, `daily_beta`, `liquidity_rank`, `overnight_beta`,
> `overnight_intraday_split`; the 5 pure-ts reference filters (`sector`, `calendar`, `asset_flags`,
> `round_levels`, `calendar_events`). A.3 hybrids: `return_dispersion`, `breadth`.

### Pattern B — windowed reducer → prior state + O(1) per-minute fold (27 groups / ~464 features)
The value is a reduction over a trailing window (sum / mean / std / OLS / EMA / extrema / cumulative).
**State = a running accumulator per (symbol, window, column).** Fold the new minute, expire the old one.
> The 23 `ReductionGroup` + 4 `StatefulGroup` already here. PLUS the ~33 hand-written groups whose bespoke
> `compute_latest` is really a bounded-window reduction not yet *declared* as one (the §2 #6 bucket).

### Pattern C — point-in-time / event (the residual hand-written groups)
Last-event, minutes-since, a small fixed lookback. **State = a tiny ring of the recent per-symbol rows** or no
state at all (pure at-T). Most are already cheap single-minute passes.

> **⚠ CORRECTION (2026-06-24, ratified with CriticalProfiler; gated by the sparse-parity test #435).** The
> last-k ring kind has TWO distinct keyings, and a Stage-3 consumer MUST pick the right one or it emits wrong
> values for every sparse (gappy) symbol:
> - **TIME / epoch-keyed** (`LastKState` in `stateful.py`): value as of minute `T − L` (null if that exact
>   minute is absent) — the `base.lagged` self-join contract. Correct for a TIME-based lag.
> - **POSITIONAL row-keyed** (the semantics `slice_derive`'s `max_lag+1` per-symbol tail / the `_matrix_at` cut
>   already use): the k-th prior PRESENT ROW (`close.shift(k).over("symbol")`), advancing only on present
>   minutes. Correct for a POSITIONAL shift. (The chosen IMPLEMENTATION of this keying for points is the
>   dedicated `PointRing` below — same semantics, a cheaper structure — not the `slice_derive` tail itself.)
>
> **The `resolve_points` point-lags are POSITIONAL `shift(k).over("symbol")`, NOT time/epoch lags** —
> CriticalProfiler's #435 proved that an epoch-keyed `LastKState` would emit wrong values for every sparse
> symbol (the L-th prior minute ≠ the L-th prior row on a gappy grid). So the Stage-3 carried-state kind for
> point-lags is a **dedicated `PointRing`** (a point-sources-only depth-121 numpy circular buffer), NOT
> `LastKState` and NOT a `slice_derive` wrapper — CriticalProfiler MEASURED the wrapper net-slower (point-lags
> reach `shift(120)` while value-derive's `max_lag` is 5, so wrapping forces a 121-deep all-column slice every
> minute; `PointRing` is 138× cheaper, 0.10ms vs 14.4ms). `PointRing` is `#435`-gated byte-identical to the
> canonical `resolve_points` truth on sparse symbols (the same proven positional invariant, via a distinct perf
> structure — equality by PROOF, not code-reuse). `LastKState` stays correct for genuine time-based lags. See
> §5 Stage 3.

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
| ~~**A2** the 5 pure-ts reference filters~~ | — | **NO-OP** (already minimal: 1 `compute()` + inherited default `compute_latest`, no cache/override boilerplate — nothing to collapse) | 0 |
| ~~**B1** `ReductionFoldState` → reuse `WindowedSumState`~~ | — | **ALREADY DONE** (it HOLDS a `WindowedSumState`, not a second fold — verified 2026-06-24) | 0 |
| ~~**B2** `CumulativeState` → infinite-window kind~~ | — | **NO-OP** (justified distinct session-reset kind; its 3-group live redundancy already consolidated by `session_cumulative_agg` #284) | 0 |
| **B3** #6 bounded-window `compute_latest` — the WHOLE-BUFFER ones | 3 cross-sectional gathers lagged/sorted the full buffer in `compute_latest` | bounded `compute_latest_on_window` / minute-slice (Stage 2a, #442) | 3 whole-buffer passes/min |
| **C1** point-in-time groups → declared at-T (no state) / `PointRing` | bespoke `compute_latest` | dedicated depth-121 `PointRing` (point-sources only, #435-gated) for positional lags, else no state | (subset of B3) |

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

- **Stage 0 — POC (MERGED, #434).** `DailySnapshotGroup` base + migrate `multi_day` + `multi_day_vwap`. Proven
  cell-identical to origin/main; fp unchanged (64 groups / 737 features, surface-hash unchanged). Proves the
  reduction is real and safe on the highest-frequency redundancy (#1-vs-#2).
- **Stage 1 — finish Pattern A.1 (DONE, this PR).** Migrated the 4 remaining pure-broadcast snapshot groups
  (`daily_beta`, `overnight_beta`, `overnight_intraday_split`, `liquidity_rank`) onto the base. Each
  `.equals()` cell-identical to origin/main; surface-hash unchanged. Surfaced the A.1/A.2/A.3 sub-shape split
  (§3): `prior_day` (A.2, needs a `broadcast_exprs` hook) deferred to **Stage 1b**; `return_dispersion` +
  `breadth` (A.3 hybrids) correctly left as-is. Generalized the base's `daily_snapshot(source, ctx)` +
  `_snapshot_witness(source, ctx)` to support the one multi-input group (`liquidity_rank`'s universe witness).
- **Stage 1b — Pattern A.2 (DONE, #439).** Added the optional `broadcast_exprs` + `minute_columns` hooks to
  the base; migrated `prior_day` (120 → 100 LOC) — its snapshot returns prior-day LEVELS and `broadcast_exprs`
  derives the close-relative features from the levels + the at-T close after the broadcast. Cell-identical to
  origin/main; surface-hash unchanged. **FINDING — the 5 reference filters need NO migration:** `sector`,
  `calendar`, `asset_flags`, `round_levels`, `calendar_events` have ZERO `compute_latest` overrides and ZERO
  cache boilerplate (a single `compute()` + the inherited default `compute_latest`). They have nothing to
  collapse — forcing them onto a base would ADD abstraction, not remove it. Confirms #381's "pure-ts filters,
  ~0 marginal cost." Left as-is by design. **Pattern A is now fully migrated** (the A.3 hybrids stay correctly
  as A-cache + B-gather).
- **Stage 2a — bound the whole-buffer cross-sectional gathers (DONE, #442).** The exhaustive group audit found
  3 groups (`peer_relative`, `cross_sectional_rank`, `return_dispersion`) whose `compute_latest` lagged / sorted
  / reduced the WHOLE minute buffer then kept only T. Rewrote each to a bounded `compute_latest_on_window` /
  minute-only slice — cell-identical to `compute().filter(T)` incl a sparse symbol, surface-hash unchanged. This
  is the real B3 win at the group level (the 3 whole-buffer offenders); the rest of the ~33 bounded-window
  groups already slice correctly (audit-confirmed CLEAN).
- **Stage 2b — collapse the B duplicates: CANCELLED (not real).** Verified 2026-06-24: `ReductionFoldState`
  already HOLDS a `WindowedSumState` (no second fold to merge); `CumulativeState` is a justified distinct
  session-reset kind whose 3-group live redundancy was already consolidated by `session_cumulative_agg` (#284).
  No churn-only refactor — the B mechanisms are already minimal. (Corrects the original §4 over-count.)
- **Stage 3 — declare the bounded-window groups (B3/C1) + the carried point/lag state (DONE, #440).** Convert
  the ~33 bespoke `compute_latest` overrides to declared windows so the engine generates the slice.
  CriticalProfiler built the `resolve_points` slice of this (the per-minute whole-buffer point/lag reads →
  carried O(1) state). **Scoping (CriticalProfiler's attribution):** of the 18 incremental groups, **15 carry
  ONLY at-T points → pure DELETION, zero state** (read `matrix_at`'s latest row, stop the whole-buffer resolve).
  Only **3** (`efficiency`, `return_dynamics`, `momentum_consistency`) need carried positional-lag state — the
  risky surface is just 3 groups; the other 15 are a clean delete. **Interface decision (ratified):** the carried
  point-lag state is a **dedicated `PointRing`** — a point-sources-only depth-121 numpy circular buffer — NOT
  `LastKState` and NOT a `slice_derive` wrapper. Two measured facts drove this: (a) #435 proved an epoch-keyed
  ring breaches every sparse symbol (the point-lags are positional `shift(k)` ROWS, not minute lags); (b)
  wrapping `slice_derive`'s tail is net-SLOWER (value-derive's `max_lag` is 5 while point-lags reach `shift(120)`,
  so the wrap forces a 121-deep ALL-column slice every minute) — `PointRing` (point columns only, depth 121) is
  138× cheaper (0.10ms vs 14.4ms). The "single sparse-correct encoding" requirement is satisfied by PROOF, not
  code-reuse: `PointRing` is `#435`-gated byte-identical to the canonical `resolve_points` truth on sparse
  symbols. The kind is `quantlib/features/point_ring.py` (#440): `PointRing(symbols, specs: list[PointSpec])`
  with `fold(minute_frame)` then `at_t(source)` / `lag(source, window)`, armed by `FP_POINT_RING=1` (default
  OFF — a gated flip, like `FP_INCREMENTAL`). Joint ownership: this design owns the declaration surface + the
  kind's taxonomy slot, CriticalProfiler owns `point_ring.py` + the `#435` gate.

Each stage is independently shippable, parity-gated, and fp-neutral. None requires a coordinated fc+strategy
deploy (no fingerprint change) — they deploy on the normal feature-computer relaunch.

---

## 6. Evidence

### Stage 0 (POC, merged #434)
- **New:** `quantlib/features/daily_snapshot_group.py` (`DailySnapshotGroup`) — owns the cache + broadcast +
  live/backfill split for Pattern A. A group implements ONLY `daily_snapshot(source, ctx)`.
- **Migrated:** `multi_day.py` (120 → 87 LOC), `multi_day_vwap.py` (91 → 68 LOC) — 4 methods → 1.

### Stage 1 (this PR)
- **Migrated** the 4 remaining pure-broadcast (A.1) snapshot groups, 4 methods → 1 each:
  `daily_beta`, `overnight_beta`, `overnight_intraday_split`, `liquidity_rank`.
- **Base generalized:** `daily_snapshot(source, ctx)` + `_snapshot_witness(source, ctx)` so a multi-input
  snapshot group (`liquidity_rank`'s universe-membership rank denominator) pairs the extra input into the cache
  witness — proven live + never stale-serving by a dedicated test.
- **Proof:** all 6 migrated groups (Stage 0 + Stage 1) are `.equals()` cell-identical to the origin/main
  four-method versions on the standard frames for both `compute()` and `compute_latest()`; the generic
  `test_fp_latest` passes for every group; the registry surface-hash is unchanged (`937c0065b9a1`, 64 groups /
  737 features) → **fp-neutral**.
- **Suite:** `test_fp_latest.py` + `test_fp_new_families.py` + `test_daily_beta.py` + `test_daily_snapshot_group.py`
  = 124 passed, 1 skipped.

### Stage 1b (this PR)
- Added `broadcast_exprs()` + `minute_columns` to the base; migrated `prior_day` (A.2, 120 → 100 LOC) —
  cell-identical to origin/main, surface-hash unchanged.
- The 5 reference filters are a **NO-OP** (already minimal, no boilerplate — see §5). **Pattern A is now fully
  migrated**; the A.3 hybrids (`return_dispersion`, `breadth`) stay as-is by design.
- Suite: 127 passed, 1 skipped.

**Pattern A complete.** Next: Stage 2 (collapse the B duplicates) and Stage 3 (the joint resolve_points surface
with CriticalProfiler).
