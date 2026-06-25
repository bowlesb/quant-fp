# One way to hold feature state — the design

> **Status: DESIGN, for sign-off before any migration.** This is the plain-language design of the single
> state abstraction Ben asked for, plus the collapse map (what we delete and what each current piece becomes).
> Nothing is migrated yet. Every claim below was verified against the live code and proven under the value gate
> (#451, merged, 184 tests) — the citations are real files/lines, not a greenfield guess.

## What Ben asked for

> *"We need a single abstraction to handle state, with no more implementations than we actually need, and
> ensuring complexity is as limited as possible so we reduce overhead."*

Three requirements, and the design is judged on them — not on speed. We measured the real numbers so we don't
overclaim: the full bar→vector path is **277ms per minute (median), of which feature compute is ~89ms (~32%)**;
the rest is store-write + IPC + bus. The compute itself is already ~42× under the per-minute budget. So this is a
**simplicity** win — measured by mechanisms and lines removed — and even the part of the time it *could* touch
(the ~32% compute third) is not the point. Throughput is a floor we must not regress, never a target.

1. **ONE abstraction to hold state.**
2. **No more implementations than we actually need** — the honest minimum, not artificial unification.
3. **As little complexity / overhead as possible.**

## Grounding: how Ben approached this before

Ben's earlier system (`automated-day-trading`) already reached for the core of this shape, and it's worth saying
so plainly because the design below is the same instinct, finished:

We read `automated-day-trading` and his approach is clear and good — and two of its ideas are *already*
load-bearing in our design, which is the strongest sign we're on the right track:

- **One uniform entry + flat numpy reads.** A bar is a `MinuteBar`/`BufferModel`
  (`scode/runner/server/minute_bar.py`) → a fixed ordered `np.ndarray` row; every feature reads from that one
  flat buffer (`scode/features/feature_vector.py:get_final_feature_vector` calls each family's getter on plain
  numpy `close`/`volume`/… arrays). That *is* "one way to pass a minute bar to a group." Kept.

- **Gaps handled by ONE mask — the idea we independently re-derived.** His getters all take an `interpolate`
  mask: a per-row flag for "was this minute real or filled". Gap/churn handling is **centralized as a mask**, not
  re-solved per feature. That is exactly our **absent-as-zero / bar-presence churn rule** — the single most
  important thing the unified container owns, and the one our prior code got wrong in seven different places. Ben
  solved it once; we adopt that, and generalize it to the per-symbol case.

- **One read-surface knob.** His getters take `last_row_only` — compute-just-the-latest vs the whole series, one
  parameter. That is our **read-surface knob** (scalar-at-T vs materialize-tail). Same idea.

**Where his design stops, and what we add (honestly).** His state lives *inside each per-family numpy function*,
driven by a Spark per-ticker partition (`scode/job/ema_features.py`: `add_feature_wrapper(fn, schema,
need_columns, agg_cols=("ticker",))`). So volume, EMA, chunk, candlestick each carry their own buffer and their
own loop — the **per-family duplication is exactly the thing Ben now wants reduced.** He did *not* have one
shared lifecycle/seed/rebuild, the proof that the positional kinds are one structure, or the explicit "minimum
fold-set" taxonomy. Those are our generalization — his per-family approach, **consolidated** onto one container
with one churn-mask (his idea), one lifecycle, and the few fold-kinds the data actually needs.

So this is **not** a greenfield invention and **not** a mirror of his repo: it is his per-family numpy approach
with the duplication removed — and his two best ideas (the gap-mask, the read knob) promoted from per-family
conventions to container guarantees. He found the fully-clean version *hard* (his words); the honest reason is
that EMA and Cumulative genuinely don't reduce to the same structure as the windowed/ring families — so the
clean answer isn't "one thing", it's "one container + the minimum folds." That matches what he found, and what
we proved.

## The problem, in one sentence

Today a feature group holds its running state through **one of seven different mechanisms, driven by two
separate engines, with four parallel "do one minute" methods** — and a new feature author has to know which of
those to reach for. That's the overhead. The state itself is simple; the number of ways to hold it is not.

### What exists today (the count Ben is reacting to)

The state/engine code spans ~4,000 lines across 10 files, of which the **state/engine *core* — the part this
design replaces — is ~1,700 lines** (the two engines + the kind classes; the rest is feature math and glue that
stays). Inside that core:

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

These are **measured** line counts against the current tree (merged main), and we're careful to separate **what
deletes** (the duplicated drive/seed/churn machinery — the real win) from **what stays but moves** (the per-kind
math, which keeps doing the same arithmetic, just plugged into one container instead of carrying its own loop).

**What DELETES — the duplication (≈900 lines):**

| today | measured LOC | becomes |
|---|---|---|
| `IncrementalEngine` + `StatefulEngine` (two engines doing the SAME seed/fold/emit lifecycle on different payloads) | **787** (543 + 244) | **ONE container drive loop** (seed + fold + read; churn + idempotency in one place) |
| the 4 `step*` twins + emit drive (`step`/`step_numpy`/`step_rust`/`step_rust_unified` + `_fold_latest`/`_latest_frame`/`_running_long`) | **~107** | **ONE read-surface dispatch** (numpy default, Rust where it pays — one path, not four) |
| `StatefulEngine`'s churn wrapper (`_fold_minute` / `_prepared_latest` / the stable-set assert `stateful.py:733` / the per-kind seed branches) | (part of the 244) | **deleted** — churn + seed handled once by the spine (the C3 win) |

**What STAYS but plugs into the container — the per-kind math (≈524 lines, loses its wrappers, keeps its arithmetic):**

| today (a "state mechanism") | measured LOC | becomes a container PAYLOAD |
|---|---|---|
| `PointRing` + `ValueInputRing` (positional) | ~202 (+ValueInputRing on #454) | **row-ring** payload; reads `{scalar-at-lag, materialize-tail}` — proven one base (#26) |
| `WindowedSumState` (additive Σ + expire) | **232** | **time-windowed row-buffer** payload; the sum is a *read* over the in-window rows (carries the Class-A/B conditioning the parked groups need) |
| `ReductionFoldState` | 75 | folds into WindowedSum (it already *is* a `WindowedSumState` wrapper) |
| `CumulativeState` (session reduce) + `ExtremaState` | 69 + 63 | **accumulator-reduce** payload (fold = reduce, reset-on-key) |
| `EMAState` (recursive) | 50 | **recursive** payload (`v = α·new + (1−α)·v`; decay gated on bar-presence) |
| `LastKState` (time-lag) | 35 | **time-keyed read** of the row-ring (+ `fill_nan(None)`) |
| `SessionCache` (snapshot) | 43 | **snapshot** payload (state = today's snapshot; fold = no-op; read = broadcast) — already minimal |
| `swing` `_SymbolLeg` (state machine) | — | **opaque state-machine** payload (`advance(value, minute) → row`) — already implements the lifecycle |

**Net (measured):** the demolition target is **~1,700 lines of state/engine core**. Of that, the **~900 lines of
duplicated drive/seed/churn/dispatch** (the two engines + the four `step*` twins + the churn wrappers) **collapse
to one container drive loop + one dispatch** — that is the real deletion. The **~524 lines of per-kind math stay**
(same arithmetic) but lose their bespoke seed/churn/lifecycle wrappers and become payloads behind one interface.
So the honest claim is **not** "delete 1,000+ lines of features" — it's "**remove ~900 lines of duplicated
plumbing and the seven-way fork**, and reduce the per-kind code to declared payloads." The win Ben cares about is
the **count of concepts**: a feature author declares `{state, fold, read}` and never touches a drive loop, a
churn rule, a seed path, or a `step` variant — *one* way to hold state instead of seven.

## How we migrate safely — the value gate makes the teardown fearless

The risk in a structural rewrite is silently changing a feature's value. We built the gate that makes that
impossible to miss: **#451 (merged, 184 tests)** asserts, for **all 64 groups × both Rust settings × both
isolated and co-resident (shared-engine) configurations**, that the live stateful path produces **byte-identical
values to the backfill source of truth**, on a deliberately gappy tape, with the degenerate cells exercised. So
the migration is mechanical and safe:

The order (lowest-risk first, each green before the next, confirmed against the real dependency edges):

1. **Build the shared spine + the positional row-ring payload.** The spine = the symbol-index + count/minute
   channel + watermark + the existing `RunningState` lifecycle. Port `PointRing` + `ValueInputRing` onto it first
   — convergence already proven (#26), two consumers, lowest risk.
2. **WindowedSum payload onto the spine** (the big one). Re-point the incremental reductions; carry the Class-A/B
   conditioning; gate the co-resident case (the straddle the gate covers).
3. **EMA / Lag / Extrema / Cumulative payloads.** Port the `StatefulEngine` kinds and **delete the stable-set
   assert + the `_prepared_latest` wrapper** — the two churn riders (fill-null, presence-gated decay) bake into
   the container fold here.
4. **swing opaque-state-machine payload** — it already implements the lifecycle + watermark, so it's the
   lowest-effort port.
5. **Collapse the four `step*` twins into one read-surface dispatch and delete the second engine.**

Each step re-runs #451 before the next; green means values survived, red means stop and fix. No feature output
changes at any step, and the fingerprint is unchanged throughout.

## What this is NOT

- **NOT a speed project.** The full bar→vector is 277ms/min, compute is ~89ms (~32%) of that and already 42×
  under budget; the demolition only touches that compute third and isn't trying to shrink it. Throughput is a
  floor we won't regress, not a target.
- **NOT "one universal ring."** EMA and Cumulative genuinely fork; forcing them in would add complexity. The
  honest minimum is one container + ~3-4 folds.
- **NOT a value change.** Every step is byte-identical to backfill, enforced by #451. The fingerprint is unchanged.
- **NOT built yet.** This is the design for sign-off. The migration starts only after Ben okays this shape.
