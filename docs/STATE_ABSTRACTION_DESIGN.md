# One way to hold feature state — the design

> **Status: DESIGN, for sign-off before any migration.** This is the plain-language design of the single
> state abstraction Ben asked for, plus the collapse map (what we delete and what each current piece becomes).
> Nothing is migrated yet. Every claim below was verified against the live code and proven under the value gate
> (#451, merged, 184 tests) — the citations are real files/lines, not a greenfield guess.

## What Ben asked for

> *"We need a single abstraction to handle state, with no more implementations than we actually need, and
> ensuring complexity is as limited as possible so we reduce overhead."*

Three requirements, and the design is judged on them — not on speed. We measured the real numbers so we don't
overclaim, and we're careful to quote them at a matched scale:

- **At sim scale (a bounded ~1,000-symbol run):** the full bar→vector path is **277ms per minute (median)**, of
  which **feature compute is ~89ms (~32%)**; the other ~68% is store-write + IPC + bus + shard contention. So
  even the part of the latency the demolition *could* touch is only about a third.
- **At full universe (~7,000 symbols, 8 shards):** the compute alone is ~1.4s per minute — comfortably ~42×
  under the 60-second budget. (Different scale from the line above — don't add them; the apples-to-apples
  "compute is ~1/3 of bar→vector" is the sim-scale pair.)

#### Why ~7,000 symbols is *not* 289ms × 7,000

A fair first reaction is: "if one ticker is ~289ms, 7,000 tickers can't be 1.4s — even on 32 cores the math
doesn't work." It doesn't, *if* cost were per-ticker. It isn't — and here is exactly why, with the measured
curve as proof:

1. **The engine is frame-vectorized — symbols are ROWS, not passes.** A shard holds *all* of its symbols as
   rows in one columnar (polars) frame, and each feature is *one expression evaluated over the whole column at
   once*. Adding a symbol adds a **row to an already-vectorized operation**, not another pass over the code.
   So the cost is **fixed overhead + a tiny marginal per-symbol**, never `per-symbol × N`.

2. **The measured scaling curve proves it (one shard, isolated):**

   | symbols on the shard | per-minute compute |
   |---|---|
   | 100 | 123ms |
   | 437 | 326ms |
   | 875 | 574ms |

   That's a straight line of **~75ms fixed + ~0.57ms per symbol** — clearly sub-linear in the way that matters.
   The line that dissolves the paradox: **100 tickers of compute is 123ms — already *cheaper* than the single
   289ms anchor.** If cost were per-ticker, 100 tickers would be ~30 *seconds*. It's 123ms. So the "× 7,000"
   mental model is simply the wrong shape for this engine.

3. **7,000 = 8 shards × ~875, and the shards run in parallel** on the 32-core box. The per-minute universe
   latency is therefore the **slowest single shard** (~574ms isolated, ~1.4s once all 8 contend for cores) —
   **not × 8, and certainly not × 7,000.**

4. **What the 289ms anchor actually is.** That number is a fixed-overhead-dominated *single-bet end-to-end*
   figure (one symbol's whole bar→vector, mostly framework/setup cost), **not** the per-ticker *compute*. So it
   should never be multiplied by symbol count — the per-symbol compute marginal is ~0.57ms, not 289ms.

**Two honest caveats (kept in plain sight, not buried):**
- The ~1.4s is **compute-only** — synthetic bars, warm buffers, with store-write / IPC / bus deliberately
  excluded (those are off the per-minute critical path). It is the cost of the part this redesign touches.
- The **full end-to-end at 7,000 is not directly measured.** We measured e2e only at the bounded ~1,000-symbol
  sim scale, where compute was ~1/3 of the 277ms bar→vector. So the honest claim is precise: *"the compute the
  redesign touches is well under budget at full universe,"* **not** *"the whole pipeline is 1.4s at 7,000."*

Either way the conclusion is the same: **this is a simplicity win** — measured by mechanisms and lines removed —
not a latency rescue. Throughput is a floor we must not regress, never a target.

1. **ONE abstraction to hold state.**
2. **No more implementations than we actually need** — the honest minimum, not artificial unification.
3. **As little complexity / overhead as possible.**

## Grounding: how Ben approached this before

Ben's earlier system (`automated-day-trading`) already reached for the core of this shape, and it's worth saying
so plainly because the design below is the same instinct, finished:

We read `automated-day-trading` and his approach is clear and good — and two of its ideas are *already*
load-bearing in our design, which is the strongest sign we're on the right track:

- **The ring buffer — read it for the DISCIPLINE, not as an abstraction to mirror.** Ben's real ring lives in
  `scode/buffer/tracker.py` (`CircularBufferOnDisk`) and its richer production sibling
  `scode/buffer/vector_store.pyx` (`VectorStore`, Cython). Ben is explicit it was *not* beautifully designed —
  what is worth extracting is the **hot-path hygiene**, made literal: *maintain aggregates incrementally on add,
  read O(1) off them, and never recompute over the buffer per minute* — plus relocating the heavy invariant work
  off the minute boundary. His `CircularBufferOnDisk` kept the hot path to ring-lookups off carried aggregates;
  the container **generalizes that discipline to every fold-kind, per-symbol, uniformly** — it does not revere or
  copy his class. Concretely, what that discipline looks like in his code (and how it maps to our fold-kinds):
  - *Positional ring with a write cursor + wraparound* — `current_position = (current_position + 1) % size`;
    physical slots, logical (time) order reconstructed as `(last_index + 1 + i) % num_rows`
    (`tracker.py:add_row`/`to_dataframe`). That is precisely our positional-row-ring (the #26 convergence
    PointRing + ValueInputRing share this one structure).
  - *O(1) reads via maintained aggregates, not re-scans (the key idea).* He never re-scans the window to answer a
    query: he keeps a hidden running `sum` row and `sum-of-squares` row (`tracker.py:add_row` updates each by
    `+= new − evicted`), so `get_sum`/`get_std` are O(1). `VectorStore` generalizes this to **per-window** dicts
    (`running_sum`, `running_sum_squares`, `max_val`, `min_val`, `running_ema_vals`) over one buffer. That is our
    **accumulator-reduce and recursive (EMA) fold-kinds living *on* the ring** — the `(state, fold)` each group
    declares, his idea, generalized.
  - *His own warmup/readiness counter.* `num_elements` increments until the buffer fills
    (`vector_store.pyx:add_value`), and `get_mean`/`get_std` divide by `adjusted_size = min(num_elements, size)`.
    That **is the readiness primitive** — "am I full yet?" answered per buffer — which our fourth element
    (`ready`) lifts to a container guarantee and reconciles with PR #165's `populated`.
  - *Extrema honestly fork.* When the evicted value was the running max/min he must re-scan the window
    (`_recalculate_min_max`, O(window)) — so min/max do **not** reduce to a pure running-fold. This is direct
    evidence for our claim that the fold-set is *small but plural*, not one universal accumulator.
  - *Seed/rebuild from the persisted buffer.* `_load_from_file` reconstructs every aggregate from the saved ring
    → his lifecycle/rebuild, our `up_to_date` / `rebuild_from_history`.

- **One uniform entry + flat numpy reads.** A bar goes through `BufferModel`
  (`scode/runner/server/minute_bar.py`): `get_ordered()` → `to_buffer_row()` produces a fixed-ordered
  `np.ndarray` row; every feature reads from that one flat buffer
  (`scode/features/feature_vector.py:get_final_feature_vector` calls each family's getter on plain numpy
  `close`/`volume`/… arrays). That *is* "one way to pass a minute bar to a group." Kept.

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
own loop — the **per-family duplication is exactly the thing Ben now wants reduced.** His ring *did* have a
per-buffer lifecycle (`_load_from_file` rebuild) and a per-buffer readiness counter (`num_elements`) — but **not
one shared container-level lifecycle/seed/rebuild across all features and symbols**, not the proof that the
positional kinds are one structure, and not the explicit "minimum fold-set" taxonomy. Those are our
generalization: we lift his per-buffer lifecycle + readiness to the *container* and run them once for every
group, instead of each buffer carrying its own. Our design is his per-family approach, **consolidated** onto one container
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

The state/engine code spans **~3,500 lines across 10 files** — two engines, seven state mechanisms, and four
`step*` variants. The *duplication* in that (the two engines + the four twins + the per-kind wrappers) is the
part this design removes; the per-kind math stays. Inside it:

- **7 state mechanisms:** `SessionCache`, `WindowedSumState`, `ReductionFoldState`, `CumulativeState`,
  `PointRing`/`ValueInputRing`, the `StatefulEngine` kinds (`EMAState` / `LastKState` / `ExtremaState`), and the
  `slice_derive` value-tail.
- **2 engines that drive them:** `IncrementalEngine` (incremental.py:375) and `StatefulEngine` (stateful.py:685)
  — each with its own seed loop, its own "advance one minute", its own churn handling.
- **4 parallel "do one minute" methods** on `IncrementalEngine` alone: `step` / `step_numpy` / `step_rust` /
  `step_rust_unified` (incremental.py:818/865/888/902) — the same operation written four ways.
- **64 feature groups across 4 base classes** (`ReductionGroup`×23, `StatefulGroup`×4, `DailySnapshotGroup`×7,
  raw `FeatureGroup`×30) — each base wiring state a slightly different way.

That is the complexity to remove.

## The design: one container, one lifecycle, a small set of folds

The whole thing collapses to **one idea**: a feature group owns a **per-symbol carried-state container**. Every
minute, the container does the same things for every group — and the *only* thing that differs between groups is
a tiny declared piece. Here is the shape:

```
A group declares FOUR small things:
  • its STATE     — what it carries per symbol (a few rows / a running sum / one decayed value)
  • its FOLD      — how one new minute updates that state  (the O(1) step)
  • its READ      — how it turns the state into the feature value at "now"
  • its READY     — when its value is VALID: how much history / what condition before "now" can be trusted

The CONTAINER (shared by ALL groups, written once) owns everything else:
  • the fixed symbol index            (who we track)
  • churn                             (a symbol shows up / disappears this minute)
  • the lifecycle                     (seed from history, rebuild when stale)
  • idempotency                       (re-seeing a minute never double-counts)
  • the readiness CHECK + handling    (ask every group's READY; withhold/flag a not-yet-ready value)
```

A minute bar enters one way (the container's `fold`), the group's declared fold updates its state in a few
operations, the container asks the group's declared `ready` whether the value is valid yet, and — if it is — the
group's declared read produces it. That's it. No engine to pick, no `step` variant to choose, no base class to
match. (The fourth element, **readiness**, is the gap Ben flagged on first read — see its own section below; it's
first-class, not a footnote.)

### The one shared part — "the spine"

Five things must behave *identically* for every group, so they live in the container, written once (the first
four below; the fifth — readiness — gets its own section after, because Ben rightly flagged it as first-class):

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
   (incremental.py:174, the unconditional buffer append at :180) and `CumulativeState`'s running sum
   double-counts it (stateful.py:173). One check in one place fixes both, for every kind.

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

### The fourth declared element — READINESS (am I warmed up yet?)

This is the gap Ben caught: **a feature must be able to say "my value isn't valid yet."** At the start of a
session a windowed feature has no window filled, an EMA hasn't seen its warmup span, a daily-snapshot feature has
no snapshot yet — and its output, if emitted, is a partial/wrong number a strategy must not trade on. The coarse
"seed from history / rebuild when stale" lifecycle can say *is the state fresh?* but **not** *is the value
trustworthy yet?* Those are different questions, and readiness is the second one.

So readiness is a **first-class, per-group declaration** — the fourth thing a group writes:

```
group.is_ready(symbol) -> bool      # is THIS symbol's value valid at "now"?
```

The condition is **per-feature, not one-size** (Ben's point): a windowed-sum is ready when its deepest window is
filled; an EMA when its warmup span has elapsed; a snapshot once the daily snapshot exists; a state-machine when
it has its first completed leg; a point-lag when the lagged bar is present. Each fold-kind/group implements its
own `is_ready`. What the **container** owns — uniformly, once — is the *checking* and the *handling*: before it
hands a value to the bus, it asks `is_ready`, and a not-ready value is **withheld or flagged, never silently
emitted as if valid.** (This closes a real bug class: a strategy trading on an under-warmed partial.)

We don't invent this — we **generalize machinery we already built** (PR #165, warm-start readiness):
- `WindowedSumState.populated(window)` (incremental.py:286) — the source-agnostic "has this window's full depth
  been absorbed?" — is exactly the windowed-sum kind's `is_ready`.
- The three-way **FULL / legitimately-not-yet-full / FAILED** distinction (`assert_ready` incremental.py:347,
  `IncrementUnderfilled` :108) is precisely the readiness vocabulary: *FULL* = ready/emit; *legitimately short*
  (first day, new ticker, real gap) = not-ready, withhold, no error; *FAILED* (history was present but didn't get
  absorbed) = a real bug, raise. The container's readiness check is this, lifted to every kind.
- The `FeatureSpec.nan_policy` already names this per feature (`"none"` / `"warmup"` / `"sparse"`, base.py:120) —
  the design makes that declaration *do* something uniformly instead of each group hand-handling it.

So the contract is **`{state, fold, read, ready}`**: a group declares its state, how to fold a minute, how to
read the value, and **when that value is valid** — and the container owns the seed, the churn, the idempotency,
**and the readiness check + not-ready handling** for all of them.

### The "no more than we need" part — exactly the folds the data demands

We tested, by construction, whether all the state shapes are really *one* thing. The honest answer — and the one
Ben asked for — is **one container with a small, closed set of fold-kinds, not a single universal ring.** We
proved which ones genuinely merge and which genuinely don't:

| fold-kind | what it carries | which of today's mechanisms it absorbs | its `is_ready` | evidence |
|---|---|---|---|---|
| **row-ring** | the last *N* per-symbol rows | `PointRing` + `ValueInputRing` + `WindowedSumState` (a row-ring **plus** a running-sum read; it keeps `_buf_vals` precisely to subtract on window-exit) | the window is filled / the lagged row is present (`populated`) | #26 (both refactored onto one base, 34 green); #27 |
| **accumulator-reduce** | one running value per symbol, reset on a key | `CumulativeState` (session min/max/sum/first) + `ExtremaState` (windowed max/min) | at least one bar absorbed since the reset key | #27 |
| **recursive** | a single decayed value (`v = α·new + (1−α)·v`) | `EMAState` | its warmup span has elapsed | #27 (genuinely forks — no rows to carry) |
| **state-machine** | a small bounded per-symbol machine | `swing` / `swing_dc` (a ZigZag leg-state machine) | its first leg/pivot has completed | pressure-test C1 |
| **snapshot** | today's per-(symbol,date) snapshot | `SessionCache` / `DailySnapshotGroup` | the daily snapshot exists for this session | — |

`EMAState` and `CumulativeState` **do not** collapse into the row-ring — an EMA keeps one number and overwrites
it; it has no rows to address (verified: `stateful.py:481`, no slot/cursor/count). Forcing them into "a ring of
depth 1" would *add* machinery they never use — the exact over-engineering Ben warns against. So the minimum
honest set is **one container + ~3-4 fold-kinds**, each declared per group, all sharing the one spine. That *is*
"no more implementations than we actually need."

### The two fix levers — and where Rust fits

A "violator" is **any non-trivial per-minute computation that didn't have to be there** — not just whole-buffer
or whole-universe recompute, but any work on the minute critical path beyond a lookup or a few ops. There are
**two first-class ways to fix one**, and they are peers — some violators are best folded, others are best
relocated:

- **L1 — fold to the ring (O(1) per minute).** The violator carries running state and folds only the new minute,
  so the read is a lookup off a maintained aggregate. This is the windowed-sum / accumulator / recursive /
  row-ring / state-machine fold-kinds above. Most violators are L1 (the parked reductions, the StatefulGroups,
  swing, momentum_run).
- **L2 — move off the hot path (precompute the intraday-invariant once, then look it up).** If the heavy part is
  *invariant across the minute* — a sector→symbol map, universe membership, a time-of-day seasonality baseline,
  daily levels — compute it **once before the minute boundary**, cache it, and let the per-minute path do nothing
  but read it. This is **not a fallback; it is a first-class lever** — the right fix for a whole class of
  violators is to *relocate* the work, not fold it. In the container this is the **snapshot payload**
  (`state = today's precomputed snapshot; fold = no-op; read = broadcast/lookup`), already realized by
  `SessionCache` / `DailySnapshotGroup`. We call it out as the L2 peer of the folds, not a degenerate corner.

  Several violators are **L1 + L2 together**: the cross-sectional Gathers (`sector_beta`, `sector_return`,
  `breadth`, `market_turbulence`, `cross_sectional_rank`) and `intraday_seasonality` have an intraday-invariant
  *input* (the sector map / membership / seasonality baseline → **L2 precompute**) and a genuinely per-minute
  *cross-sectional reduce* over the live universe (→ **L1 fold**, computed once per minute on a shared base and
  broadcast). Fixing them means doing both, and the container needs the snapshot lever as a peer so the L2 half
  has a home.

- **Rust where a minute still can't reduce to a few ops:** unchanged from today's good parts — the tape/tick
  kernels and the reduction kernels that already pay off stay; the design just gives them one fold interface to
  plug into instead of four `step_*` twins. Rust is the *implementation* of an L1 fold when the fold itself is
  heavy, not a third lever.

## The collapse map — from 7+2+4 to 1+3

These are **measured** line counts against the current tree (merged main), and we're careful to separate **what
deletes** (the duplicated drive/seed/churn machinery — the real win) from **what stays but moves** (the per-kind
math, which keeps doing the same arithmetic, just plugged into one container instead of carrying its own loop).

**What DELETES — the duplication:**

| today | measured LOC | becomes |
|---|---|---|
| `IncrementalEngine` (555) + `StatefulEngine` + its emit drive (~244) — two engines doing the SAME seed/fold/emit lifecycle on different payloads | **~800** | **ONE container drive loop** (~200–250 lines of new code; seed + fold + read; churn + idempotency in one place) → **net ~550 removed** |
| the 4 `step*` twins + emit drive (`step`/`step_numpy`/`step_rust`/`step_rust_unified` + the latest-frame helpers, ~112) | **~112** | **ONE read-surface dispatch** (~25 lines) → **net ~87 removed** |
| `StatefulEngine`'s churn wrapper (`_fold_minute` / `_prepared_latest` / the stable-set assert `stateful.py:733` / the per-kind seed branches) | ~40 | **deleted** — churn + seed handled once by the spine (the C3 win) |
| `ReductionFoldState` (already just a `WindowedSumState` wrapper) | **75** | folds into the windowed-sum payload — nothing new |

**What STAYS but plugs into the container — the per-kind math (the (state, fold) a group declares; keeps its arithmetic, loses its wrappers):**

| today (a "state mechanism") | measured LOC | becomes a container PAYLOAD |
|---|---|---|
| `PointRing` + `ValueInputRing` (positional) | ~65 (+~80 `ValueInputRing` on #454) | **row-ring** payload; reads `{scalar-at-lag, materialize-tail}` — proven one base (#26) |
| `WindowedSumState` (additive Σ + expire) | **232** | **time-windowed row-buffer** payload; the sum is a *read* over the in-window rows (carries the Class-A/B conditioning the parked groups need) |
| `CumulativeState` (session reduce) + `ExtremaState` | 69 + 63 | **accumulator-reduce** payload (fold = reduce, reset-on-key) |
| `EMAState` (recursive) | 50 | **recursive** payload (`v = α·new + (1−α)·v`; decay gated on bar-presence) |
| `LastKState` (time-lag) | 35 | **time-keyed read** of the row-ring (+ `fill_nan(None)`) |
| `SessionCache` (snapshot) | 31 | **snapshot** payload (state = today's snapshot; fold = no-op; read = broadcast) — already minimal |
| `swing` `_SymbolLeg` (state machine) | — | **opaque state-machine** payload (`advance(value, minute) → row`) — already implements the lifecycle |

**Net (measured), counted honestly.** The state/engine code is ~3,500 lines across 10 files. The genuinely-removed
part is the **duplication** — but we count it as the *net* removed, because the engines don't just vanish, they're
*replaced* by one smaller container drive loop. So: the two engines (~800) become one drive loop (~200–250) ≈
**~550 net removed**; the four `step*` twins (~112) become one dispatch (~25) ≈ **~87 removed**;
`ReductionFoldState` disappears (already a `WindowedSumState` wrapper, ~75 — but its math is *kept* in the
windowed-sum payload, so it's a dedup not a deletion); and the churn wrappers + stable-set assert + per-kind seed
branches go ≈ **~40 removed**. That totals **~650–700 lines of duplicated drive/seed/churn/dispatch removed.**
The per-kind math (~553 lines: WindowedSum 232, ReductionFold 75, Cumulative 69, Extrema 63, EMA 50, LastK 35,
SessionCache 29) **stays** — it's the `(state, fold)` each group declares, now behind one interface instead of
seven.

Stated as one line: **≈900 lines of duplicated plumbing (the two engines ~800 + four `step*` twins ~112)
collapse into a single ~210-line container drive loop + dispatch — a net reduction of ~680 lines, and seven
mechanisms to one.**

But the honest headline is **not the line count — it's the count of concepts.** Seven ways to hold state, two
engines, and four `step*` variants become **one container + a handful of declared folds.** A feature author
writes `{state, fold, read, ready}` and never touches a drive loop, a churn rule, a seed path, a readiness check,
or a `step` variant.
*That* is what "no more implementations than we actually need" and "complexity no one can follow → complexity
anyone can" mean in practice. The lines are a side effect; the followability is the point.

## How we migrate safely — the value gate makes the teardown fearless

The risk in a structural rewrite is silently changing a feature's value. We built the gate that makes that
impossible to miss: **#451 (merged, 184 tests)** asserts, for **all 64 groups × both Rust settings × both
isolated and co-resident (shared-engine) configurations**, that the live stateful path produces **byte-identical
values to the backfill source of truth**, on a deliberately gappy tape, with the degenerate cells exercised. So
the migration is mechanical and safe:

The order (lowest-risk first, each green before the next, confirmed against the real dependency edges):

1. **Build the shared spine + the positional row-ring payload.** The spine = the symbol-index + count/minute
   channel + watermark + the existing `RunningState` lifecycle + **the readiness check** (lift `populated` /
   `assert_ready`'s three-way FULL/short/FAILED to the container). Port `PointRing` + `ValueInputRing` onto it
   first — convergence already proven (#26), two consumers, lowest risk.

Every ported kind also carries its **`is_ready`** (step 1 establishes the container check; each later step's kind
declares its own condition — windowed-sum = window filled, EMA = warmup span, snapshot = snapshot exists, etc.).
2. **WindowedSum payload onto the spine** (the big one). Re-point the incremental reductions; carry the Class-A/B
   conditioning. There is one known shared-engine hazard — two groups that share the same engine, where a
   numerical quirk in one (`price_volume`) could corrupt the other (`return_dynamics`); the value gate catches it.
   It lives *entirely inside this step* (both are windowed-sum groups), so there's no co-residency constraint that
   crosses steps — this step just migrates its groups together and stays co-resident-gated by #451 internally.
3. **EMA / Lag / Extrema / Cumulative payloads.** Port the `StatefulEngine` kinds and **delete the stable-set
   assert + the `_prepared_latest` wrapper** — the two churn riders (fill-null, presence-gated decay) bake into
   the container fold here.
4. **swing opaque-state-machine payload** — it already implements the lifecycle + watermark, so it's the
   lowest-effort port.
5. **Collapse the four `step*` twins into one read-surface dispatch and delete the second engine.**

Each step re-runs #451 before the next; green means values survived, red means stop and fix. No feature output
changes at any step, and the fingerprint is unchanged throughout.

## How we'll know it worked — and how latency work gets easy afterward

The point of this isn't a number going down; it's, in Ben's words, *"consolidation, good design, reducing
duplication, and being able to quickly profile and improve latency with a minimal amount of code that needs to be
improved"* — and not having *"latencies super high because of a web of unneeded complexity no one can follow."*
So success is measured as: **how many mechanisms we consolidated, how much duplication we removed, and whether a
person can follow where the time goes.** The 42× headroom isn't the goal — it's *why* we can afford to do this
right instead of firefighting.

The second half of that goal — "quickly profile and improve latency" — is something we already have the tools
for, and the consolidation is what makes them *useful*. Two profilers stay as the **permanent scoreboard**:

- **`live_throughput.py`** — the one-command, honest, full-universe number. It times the *real* live path
  (`process_bars`, the actual per-minute compute every group does), at any scale, and reports the universe
  per-minute latency as the slowest shard's steady-state time. This is the **no-regress floor**: run it before
  and after each migration step; the number must not get worse.
- **`phase_profile.py`** — the micro-profiler that opens one group's minute into its handful of phases (the
  buffer derive, the actual fold arithmetic, the point/lag pass, the assemble) and shows where the time really
  is. This is the tool that proved the live cost is ~1% arithmetic and ~86% polars-shuffling — i.e. the time is
  in the *plumbing*, not the math.

Here is the payoff, stated plainly: **today**, that plumbing is smeared across two engines × four `step*` twins ×
a per-kind wrapper each — so "why is this slow?" means tracing a path through a web no one fully holds in their
head (exactly Ben's complaint). **After** the consolidation, there is one container drive loop and a few folds —
so improving latency becomes: *run `live_throughput` to see the universe number → run `phase_profile` to see
which of the few phases dominates → fix that one place.* The consolidated codebase is the "minimal amount of code
that needs to be improved"; the two profilers are the "quickly profile" half. That capability — not a ms target
— is the deliverable.

## What this is NOT

- **NOT a speed project.** At sim scale the full bar→vector is 277ms/min and compute is ~89ms (~32%) of it; at
  full universe the compute is ~1.4s/min, ~42× under budget. The demolition only touches the compute fraction
  and isn't trying to shrink it. Throughput is a floor we won't regress, not a target.
- **NOT "one universal ring."** EMA and Cumulative genuinely fork; forcing them in would add complexity. The
  honest minimum is one container + ~3-4 folds.
- **NOT a value change.** Every step is byte-identical to backfill, enforced by #451. The fingerprint is unchanged.
- **NOT built yet.** This is the design for sign-off. The migration starts only after Ben okays this shape.
