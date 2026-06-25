# One way to hold feature state — the design

> **Status: DESIGN, for sign-off before any migration.** This is the plain-language design of the single
> state abstraction Ben asked for, plus the collapse map (what we delete and what each current piece becomes).
> Nothing is migrated yet. Every claim below was verified against the live code and proven under the value gate
> (#451, merged, 184 tests) — the citations are real files/lines, not a greenfield guess.

## What Ben asked for

> *"We need a single abstraction to handle state, with no more implementations than we actually need, and
> ensuring complexity is as limited as possible so we reduce overhead."*

Three requirements, and the design is judged on them — not on speed (we measured: the compute is already ~42×
under the per-minute budget, so this is a **simplicity** win, measured by mechanisms and lines removed, with
throughput as a floor we must not regress, not a target):

1. **ONE abstraction to hold state.**
2. **No more implementations than we actually need** — the honest minimum, not artificial unification.
3. **As little complexity / overhead as possible.**

## Grounding: how Ben approached this before

Ben's earlier system (`automated-day-trading`) already reached for the core of this shape, and it's worth saying
so plainly because the design below is the same instinct, finished:

- **One uniform entry for a minute bar.** There, a bar is a `MinuteBar`/`BufferModel`
  (`scode/runner/server/minute_bar.py`) with a fixed ordered field list and a single
  `to_buffer_row()` → flat `np.ndarray`. Every feature reads from that one flat buffer
  (`scode/features/feature_vector.py:get_final_feature_vector` operates on plain numpy close/volume/… arrays) —
  not a bespoke per-feature frame. That is exactly the "ONE way to pass a minute bar to a group" Ben wants, and
  the design keeps it: the container's fold takes one ordered minute row per symbol.
- **State as a carried buffer, read by cheap numpy.** The streaming path carried a flat buffer and the features
  were numpy reductions over it — the same "carry a little, fold cheap, read at T" loop this design generalizes
  to a per-symbol container.

The honest difference: Ben found a fully-clean single design *hard* (his words), and our current system grew
seven mechanisms solving the same loop seven ways. This design takes his uniform-buffer instinct and makes it
*the one* way, with the minimum set of fold-kinds the data actually requires.

## The problem, in one sentence

Today a feature group holds its running state through **one of seven different mechanisms, driven by two
separate engines, with four parallel "do one minute" methods** — and a new feature author has to know which of
those to reach for. That's the overhead. The state itself is simple; the number of ways to hold it is not.

### What exists today (the count Ben is reacting to)

The state/engine machinery is **~4,086 lines across 10 files**. Inside it:

- **7 state mechanisms:** `SessionCache`, `WindowedSumState`, `ReductionFoldState`, `CumulativeState`,
  `PointRing`/`ValueInputRing`, the `StatefulEngine` kinds (`EMAState` / `LastKState` / `ExtremaState`), and the
  `slice_derive` value-tail.
- **2 engines that drive them:** `IncrementalEngine` (incremental.py:375) and `StatefulEngine` (stateful.py:685)
  — each with its own seed loop, its own "advance one minute", its own churn handling.
- **4 parallel "do one minute" methods** on `IncrementalEngine` alone: `step` / `step_numpy` / `step_rust` /
  `step_rust_unified` (incremental.py:818/865/888/902) — the same operation written four ways.
- **65 feature groups across 4 base classes** (`ReductionGroup`×23, `StatefulGroup`×4, `DailySnapshotGroup`×7,
  raw `FeatureGroup`×31) — each base wiring state a slightly different way.

That is the complexity to remove.

## The design: one container, one lifecycle, a small set of folds

The whole thing collapses to **one idea**: a feature group owns a **per-symbol carried-state container**. Every
minute, the container does the same three things for every group — and the *only* thing that differs between
groups is a tiny declared piece. Here is the shape:

```
A group declares THREE small things:
  • its STATE     — what it carries per symbol (a few rows / a running sum / one decayed value)
  • its FOLD      — how one new minute updates that state  (the O(1) step)
  • its READ      — how it turns the state into the feature value at "now"

The CONTAINER (shared by ALL groups, written once) owns everything else:
  • the fixed symbol index            (who we track)
  • churn                             (a symbol shows up / disappears this minute)
  • the lifecycle                     (seed from history, rebuild when stale)
  • idempotency                       (re-seeing a minute never double-counts)
```

A minute bar enters one way (the container's `fold`), the group's declared fold updates its state in a few
operations, the group's declared read produces the value. That's it. No engine to pick, no `step` variant to
choose, no base class to match.

### The one shared part — "the spine"

Four things must behave *identically* for every group, so they live in the container, written once:

1. **Fixed symbol index + churn.** We track a fixed set of symbols. When a symbol has no bar this minute it
   simply doesn't contribute — exactly what the backfill (the source of truth) does when there's no row. We
   verified every existing state kind already does this against a fixed index; the *only* thing that made
   `StatefulGroup`s unable to handle a gappy symbol stream was **one line** — an assertion that the symbol set
   never changes (`stateful.py:733`). Delete that line + fill absent symbols with "nothing", and churn is
   uniform for free. (Proven value-identical under the gate on a gappy tape, and a no-op on the dense path.)

2. **The lifecycle.** "Seed from history, and rebuild if you've gone stale (cold start / a gap / a code swap)."
   This contract already exists on the group base (`running_state.py`, `base.FeatureGroup.up_to_date` /
   `rebuild_from_history`) — every kind routes through it instead of hand-rolling its own seed.

3. **Idempotency (a minute is absorbed once).** Each container carries the epoch of the last minute it absorbed;
   a fold of a minute it's already seen is skipped. This makes a reconnect/replay safe **by construction** — and
   it closes two real silent bugs we found: `WindowedSumState.update` re-adds a re-delivered minute
   (incremental.py:120) and `CumulativeState`'s running sum double-counts it (stateful.py:173). One check in one
   place fixes both, for every kind.

4. **Variable-height rebuild on churn.** When some symbols are present and some aren't, the container carries a
   per-symbol count + the minute each row belongs to, and reconstructs exactly the rows that are really there —
   identical to what slicing the buffer produces, but cheaply (carry numbers, not frames). We proved this
   byte-identical on a real engine path (#454).

Two small **churn riders** also live in the container (both proven value-identical AND no-ops on the dense path):
- **null vs NaN:** the emit fills absent-cell NaNs to proper nulls (the same fix already used elsewhere, #448).
- **decaying state on gaps:** a recursive/chained value (an EMA-of-an-EMA, like MACD's signal line) must decay
  once per *present bar*, not once per clock minute — so the container hands each fold a "did this symbol get a
  real bar this minute" flag, and the recursive folds use it. (Without this a gappy symbol's MACD silently
  drifts; with it, it's identical to the backfill. Verified.)

### The "no more than we need" part — exactly the folds the data demands

We tested, by construction, whether all the state shapes are really *one* thing. The honest answer — and the one
Ben asked for — is **one container with a small, closed set of fold-kinds, not a single universal ring.** We
proved which ones genuinely merge and which genuinely don't:

| fold-kind | what it carries | which of today's mechanisms it absorbs | evidence |
|---|---|---|---|
| **row-ring** | the last *N* per-symbol rows | `PointRing` + `ValueInputRing` + `WindowedSumState` (a row-ring **plus** a running-sum read; it keeps `_buf_vals` precisely to subtract on window-exit) | #26 (both refactored onto one base, 34 green); #27 |
| **accumulator-reduce** | one running value per symbol, reset on a key | `CumulativeState` (session min/max/sum/first) + `ExtremaState` (windowed max/min) | #27 |
| **recursive** | a single decayed value (`v = α·new + (1−α)·v`) | `EMAState` | #27 (genuinely forks — no rows to carry) |
| **state-machine** | a small bounded per-symbol machine | `swing` / `swing_dc` (a ZigZag leg-state machine) | pressure-test C1 |

`EMAState` and `CumulativeState` **do not** collapse into the row-ring — an EMA keeps one number and overwrites
it; it has no rows to address (verified: `stateful.py:481`, no slot/cursor/count). Forcing them into "a ring of
depth 1" would *add* machinery they never use — the exact over-engineering Ben warns against. So the minimum
honest set is **one container + ~3-4 fold-kinds**, each declared per group, all sharing the one spine. That *is*
"no more implementations than we actually need."

### Where Rust and pre-minute work fit (Ben's other two asks)

- **Rust where a minute can't reduce to a few ops:** unchanged from today's good parts — the tape/tick kernels
  and the reduction kernels that already pay off stay; the design just gives them one fold interface to plug
  into instead of four `step_*` twins.
- **Pre-minute-boundary work for intraday-invariants:** a Class-A "snapshot" group (daily levels, sector, etc.)
  computes once per session and broadcasts — already true via `SessionCache`/`DailySnapshotGroup`; in the unified
  shape it's simply the degenerate fold ("state = today's snapshot; fold = no-op; read = broadcast").

## The collapse map — from 7+2+4 to 1+3

| today | lines (approx) | becomes |
|---|---|---|
| `IncrementalEngine` + `StatefulEngine` (2 engines, 2 seed loops, 2 drive loops) | ~1,300 | **ONE container drive loop** (seed + fold + read, churn/idempotency in one place) |
| `step` / `step_numpy` / `step_rust` / `step_rust_unified` (4 twins) | ~250 | **ONE fold dispatch** (numpy default; Rust where it pays — one path, not four) |
| `PointRing` + `ValueInputRing` (2 positional rings) | ~260 | **the row-ring fold** (one base; the two are configs `depth=121/cols=points` vs `depth=6/cols=values`) — already proven (#26) |
| `WindowedSumState` + `ReductionFoldState` | ~970 | **row-ring + a windowed-sum read** (the sum is a read over the ring's in-window rows; the conditioning the parked groups need rides here) |
| `CumulativeState` + `ExtremaState` | ~250 | **the accumulator-reduce fold** (reset-on-key) |
| `EMAState` + `LastKState` | ~250 | **the recursive fold** (EMA) + **a time-keyed read** of the row-ring (Last-K) |
| `StatefulEngine` stable-set assert (`stateful.py:733`) | 1 line | **deleted** — churn handled by the spine |
| `SessionCache` / `DailySnapshotGroup` | ~250 | **the snapshot degenerate fold** (kept; it's already the minimal form) |

**Net:** the **7 mechanisms + 2 engines + 4 step twins** become **1 container + ~3-4 fold-kinds + 1 fold
dispatch.** A new feature author declares `{state, fold, read}` and never touches the drive loop, the churn
rule, the seed logic, or a `step` variant. Conservatively this removes **well over 1,000 lines** of duplicated
drive/seed/churn/dispatch machinery; the exact number CriticalProfiler will pin against the tree, but the win is
the **count of concepts**, not just lines: *one* way to hold state instead of seven.

## How we migrate safely — the value gate makes the teardown fearless

The risk in a structural rewrite is silently changing a feature's value. We built the gate that makes that
impossible to miss: **#451 (merged, 184 tests)** asserts, for **all 64 groups × both Rust settings × both
isolated and co-resident (shared-engine) configurations**, that the live stateful path produces **byte-identical
values to the backfill source of truth**, on a deliberately gappy tape, with the degenerate cells exercised. So
the migration is mechanical and safe:

1. Build the one container + the ~3-4 folds (the positional row-ring base already exists and is proven, #26/#454).
2. Move one fold-kind's groups onto it at a time; re-run #451; green means values survived. Red means stop.
3. When all groups are on it, delete the two engines, the four step twins, and the redundant state classes.

Each step is value-gated by #451 and changes no feature output. Suggested order (lowest risk first, each green
before the next): **snapshot → row-ring (points already live) → windowed-sum (carries the conditioning) →
accumulator/recursive → swing.** CriticalProfiler will confirm the exact order against the dependency edges.

## What this is NOT

- **NOT a speed project.** Compute is 42× under budget; throughput is a floor we won't regress, not a target.
- **NOT "one universal ring."** EMA and Cumulative genuinely fork; forcing them in would add complexity. The
  honest minimum is one container + ~3-4 folds.
- **NOT a value change.** Every step is byte-identical to backfill, enforced by #451. The fingerprint is unchanged.
- **NOT built yet.** This is the design for sign-off. The migration starts only after Ben okays this shape.
