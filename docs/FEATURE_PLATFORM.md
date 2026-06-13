# Feature Platform — Vision, Requirements & Engineering Milestones

**Status:** PROPOSED (Ben's directive, 2026-06-12). Supersedes the edge-first framing as the
team's PRIMARY organizing spine. Edge/modelling continues as a parallel, downstream track
(see §9). This document is the contract: an agent's work is judged against the milestone
exit criteria here, verbatim.

This doc is written to be **impossible to lazily half-satisfy.** Every exit criterion below
carries (a) a hard number, (b) the exact command/query that proves it, and (c) the evidence
artifact that must be committed. The **Global Anti-Gaming Rules (§6)** apply to every single
criterion and override any local convenience. Read §6 before claiming anything green.

---

## 1. North Star — what we are building

> **A feature platform that, at the beginning of every minute, for every ticker in the
> universe, has a complete point-in-time feature vector available — where every feature is a
> deterministic function of all market data up to that minute — computed fast enough to act on,
> with a hard guarantee that the live value equals the backfilled value.**

In one sentence: *make a trustworthy decision for any ticker, at the start of any minute of the
day, from features we can equally compute live or over history.*

The platform's success is an **engineering** outcome (it works, it's fast, it's parity-true,
it's introspectable), **not** a market outcome (it found edge). Edge rides on top once the
platform exists. We build the predictable thing first.

### 1.1 Greenfield mandate — the existing system is NOT a constraint
**Do not be biased toward what already exists.** The current services (ingestor, executor,
model-server, feature-computer, scheduler, …), the **TimescaleDB schema and all its data**, the
storage layout, the retention policy, and the existing ~25 features are **THROWAWAY** if they
don't serve this design. Treat this as a **greenfield build that happens to have a running
reference implementation** — not a system to preserve.

- **Rebuild freely.** The database (tables, hypertables, dimensions, retention), the capture
  processes, the aggregation path, the feature-compute path, and the storage model can all be
  redesigned or replaced wholesale. Wiping and re-architecting the DB is on the table.
- **Reuse only on merit.** Keep an existing piece ONLY when it genuinely fits the architecture
  here — never because it's already there, already works, or rebuilding feels wasteful. Sunk cost
  is not a reason; "it already runs" is not a reason.
- **What actually carries forward is the PRINCIPLES**, not the code: one-function live==backfill
  parity (§3.1), evidence-first/anti-gaming gates (§6), point-in-time correctness, the shared
  capture data feed. Everything downstream of those is the new design's to choose.

When a current implementation and this document disagree, **this document wins and the
implementation gets rebuilt** — do not contort the design to fit the incumbent infra.

### 1.2 The prime directive — the full feature set AND parity, together, from day one
The single hardest requirement, and the one both prior efforts failed: **design the system from
the very start to support the FULL, complex feature set (everything in §11 — sub-minute
microstructure bursts, trade-flow, quote/spread, cross-sectional, multi-day) WHILE keeping an
ironclad real-time↔backfill parity guarantee.** Not one or the other — **both, by construction.**

- **Design for the hard features now, not later.** Do NOT build something that handles a few
  simple features and "adds complexity later." The architecture must accommodate the hardest
  features (sub-minute, stateful, cross-sectional, multi-day) on day one — parity is only
  trustworthy if it holds for the complex ones, and an architecture that can't will have to be
  thrown away (§1.1) the moment a complex feature arrives.
- **The end goal every decision serves:** collect a complex set of features in real time,
  backfill the SAME features identically, **train a model on the backfilled features, and run that
  model live on real-time features computed by the same code — with ZERO train/serve skew.**
- **Parity is the spine, not a test bolted on.** If a feature cannot be computed identically live
  and from backfill, it does not belong in the platform — redesign it until it can, or drop it.

This is the prime directive. §3.1 (one-function parity) and FP3 (the T+1 Settled-Day Parity Test)
are how it is enforced; §11 is the full scope it must hold across from the start.

### 1.3 The feature development lifecycle — and the definition of winning
This is the loop the entire platform exists to make cheap, repeatable, and trustworthy. **If we
cannot demonstrate this full loop, end-to-end, REPEATEDLY, for new features — we are NOT winning.**
Everything in FP0–FP4 is scaffolding for this cycle. For every new feature (or feature group):

1. **Bring in the code.** Implement it as a `FeatureGroup` — one file, declared inputs + windows +
   descriptions + contracts (§3.4).
2. **Collect live for ALL tickers, first.** Turn it on in real time across the full universe and
   all sessions — collection comes BEFORE any backfill.
3. **Verify the speed contract.** It computes within the latency budget at full scale (R7/R8):
   the whole vector stays p99 ≤ 2 s, the group honors its `latency_budget`. Too slow ⇒ it does not
   ship; fix or drop.
4. **Verify the day's distributions.** `make introspect`: the realized distribution is sane and
   **consistent with the feature's description** — a "frequency" never negative, a ratio in [0,1],
   ranges plausible, `n_unique ≥ 2`.
5. **QA pokes at it — adversarially, with common sense.** The QA agent actively hunts for LOGICAL
   errors in the collection: does the value move the way the name claims? do bursts appear on names
   that actually burst? are sign, units, and magnitude right? are extreme cells real or bugs? This
   is human-judgment scrutiny whose job is to BREAK the feature, not a threshold that rubber-stamps it.
6. **Throw out and re-collect on any doubt.** If anything looks wrong, discard the collected data
   and collect again after the fix. A feature is provisional until it survives this.
7. **Audit the NaNs.** Find every cell where the feature could not be computed and decide, per
   case, whether that NaN MAKES SENSE (genuine warmup / tradeless minute / undefined math) or hides
   a bug. NaNs are explained, never waved through.
8. **Selective backfill, parity-matched.** Once we like it live, backfill THAT specific feature /
   group (selective — not the whole world) and prove it **positively matches** what we computed
   live: the T+1 Settled-Day Parity Test ≥ tolerance, per tier, per session (§3.5).
9. **Train a production model on the backfill, run it live.** Develop a model on the backfilled
   features and serve it on the real-time features computed by the identical code — zero skew.
10. **Repeat.** The platform's whole value is that steps 1–9 stay cheap and trustworthy feature
    after feature.

**The definition of winning:** a demonstrated ability to run this entire flow for new features,
**repeatedly**. Any milestone or task that does not move us toward demonstrating this loop
end-to-end is not the priority.

---

## 2. The two hard-won lessons this design exists to honor

1. **Effort A (rich offline feature store, Spark/Delta):** great backfill, but no guarantee the
   production-computed features matched the backfilled ones. Result: untrustworthy.
2. **Effort B (rich real-time feature computer):** great live features, but no way to backfill
   them with confidence they matched. Result: untrustworthy.

**Root cause both times: live and backfill were two different computations.** This platform
makes them the *same* computation evaluated at different times (§3). Parity is then a property
**by construction**, monitored — not hoped for.

---

## 3. Architecture (the model every milestone serves)

### 3.1 The one-function parity model
A feature is **defined** as a pure function `f(ticker, T)` of all ticks with timestamp `≤ T`.
- **Live** = evaluate `f(ticker, now)` at each minute boundary.
- **Backfill** = evaluate `f(ticker, T)` for any historical minute `T`.

Same function, same stored inputs → live and backfill cannot structurally diverge. The only
residual gap is input differences between the real-time and historical data feeds (corrections,
late prints); that residual is *measured daily* and bounded at the parity tolerance (§4, FP3).

### 3.2 The four persistent layers (source of truth flows downward)
1. **Raw ticks** — every trade and every quote-change, for the full universe. Rolling window
   sized to disk (§4). Purpose: re-deriving *new* micro-features over history after we invent
   them. The capture process contains **zero feature logic** (parity protection).
2. **Per-minute aggregates** — the canonical, permanent substrate. One row per `(ticker,
   minute)` holding the last-minute roll-up (trade frequency, quote frequency, signed volume,
   up/down-tick counts, burst statistics, spread, …). **Columnar and per-feature-addressable**
   — this is what lets us recompute/update ONE feature without rebuilding the vector.
3. **Feature vectors** — a *derived view* over the aggregates plus longer trailing windows.
   Never a source of truth; always recomputable from layers 1–2.
4. **Decisions** — minute-cadence only. Sub-minute richness enters as *features*, not as a
   sub-second execution loop (see Non-Goals, §8).

**Sessions & the minute grid.** The minute grid spans the FULL trading day, **04:00–20:00 ET**,
not just RTH. Every minute (and every aggregate/vector row) carries a
`session ∈ {premarket (04:00–09:30), regular (09:30–16:00), afterhours (16:00–20:00)}`. Capture,
backfill, aggregates, and features cover **all three sessions from day one** — no RTH-only
hardcoding anywhere in the capture or feature path. Features may condition on `session`;
decisions may be restricted to chosen sessions by policy, but the data is always collected and
backfilled so the choice is ours later, not forced by a gap.

### 3.3 Implementation substrate (guidance, not mandate)
The implementing agents choose the stack. Two suggestions, with reasons — not requirements:
- For feature/aggregation/ML logic, a **vectorized columnar engine** (e.g. Polars — Rust-backed
  but driven from Python) fits well: 500×10k×≤2 s is a columnar batch problem, and staying in one
  Python+ML ecosystem keeps a single parity codepath.
- For the raw-capture firehose, **if** the FP1.c throughput gate shows sharded Python can't keep
  up at the open-bell burst, a lower-overhead capture layer (e.g. Rust) is a reasonable
  escalation — for *capture only*.

**The one hard constraint (an invariant, not a tool choice):** a feature must be computed by
**exactly one codepath shared by live and backfill.** Whatever stack is chosen, do not split live
and backfill feature logic across two implementations — that is the §2 failure mode. A capture
layer that only writes raw ticks (no feature math) does not violate this.

### 3.4 Feature groups — the unit of extension
A **FeatureGroup** is the primary building block and the unit an agent adds. It is a cohesive
batch of related features that share inputs and computation. Individual **features** are the
named, addressable *outputs* of a group.

Why the group, not the lone feature, is the unit:
- **Efficiency (load-bearing for the ≤2 s budget):** related features share intermediates — an
  order-flow group derives the signed-flow series ONCE, then emits `ofi_5m/15m/30m` from it.
  Per-feature recomputation of shared work would blow the latency budget.
- **Fault isolation:** groups are independent. The engine runs each separately; one group raising
  marks *its own* features unavailable-and-FAILING (loud, owned) for that minute — it never
  corrupts or silently substitutes another group's values, and never takes the vector down.
  (Isolation, not graceful degradation: failures are surfaced, never hidden.)
- **Ownership & extension:** one group = one module file = one owner. Agents extend the platform
  by adding a group file; they never edit a shared compute function, so the hot path has no
  merge-collision surface.

A group declares, and the engine ENFORCES, a contract:
```python
class FeatureGroup:
    name: str                     # unique; registration rejects duplicates
    version: str                  # semver; a bump = a new versioned group (see 3.6)
    owner: str                    # role
    inputs: list[InputSpec]       # declared deps: which layers/tables + trailing windows
    features: list[FeatureSpec]   # named outputs; each: description, dtype, valid range,
                                  #   nan policy, parity_tolerance (default 0.95), latency_budget
    def compute(self, ctx: BatchContext) -> "DataFrame":
        """Vectorized over ALL requested (symbol, minute) cells. Returns a frame keyed by
        (symbol, minute) with exactly one column per declared feature. The SAME method serves
        live (minute = now) and backfill (minutes = a historical range) — the one-function
        parity model of 3.1, batched."""
```
The engine validates every returned frame against the declared `features` (columns present,
dtypes, ranges, nan policy) and **rejects** a non-conforming group — a group cannot silently emit
an undeclared column, a wrong dtype, or an out-of-range value into the store.

### 3.5 Public interfaces (the stable API surface)
These signatures are the contract agents code against; they change rarely and only via review.

**Read — extract any feature(s), any timeframe, with a sane, defined result:**
```python
def get_features(
    names: list[str],                 # feature or group names
    symbols: list[str] | "universe",
    start: datetime, end: datetime,
    freq: str = "1m",
) -> "DataFrame":
    """Tidy frame keyed (symbol, minute), one column per requested feature, sorted, point-in-time
    (each value uses only data ≤ its minute), IDENTICAL whether produced live or by backfill.
    Contract: stable column order, declared dtypes, NaN ONLY where by-construction (reason
    queryable) — never silent zeros. RAISES on an unknown/uncertified feature or an
    out-of-retention range, rather than returning garbage."""
```

**Register — define a feature group (the extension entry point):**
```python
@register                              # validates + adds to the registry at import time
class TradeFlowGroup(FeatureGroup): ...
```

**Ad-hoc parity test — "would my feature have matched what we collected live yesterday?":**
```python
def parity_test(
    target: str,                       # feature or group name
    day: date | None = None,           # default: last settled day
    symbols=..., tier=...,
) -> "ParityReport":
    """Recompute `target` via backfill over `day` from stored raw ticks, compare CELL-BY-CELL
    against the values the running system collected LIVE that day, and return per-feature,
    per-tier match %, compared-cell count, and sampled mismatches. This is BOTH the developer
    tool an agent runs before proposing a feature AND the engine behind the daily FP3 gate."""
```
CLI mirror: `make parity DAY=<date> TARGET=<group>`.

**★ The cornerstone test — the T+1 Settled-Day Parity Test (first-class; THE test of this system).**
This is the §2 lesson, automated, and the single most important check in the platform. On day
**D+1**, after the historical API has *settled* day D (overnight corrections and cancellations
applied — which is exactly why it runs the next day, not same-day), re-fetch D from the historical
API, recompute **every** feature with the one shared codepath, and diff **cell-by-cell against
what the running system collected LIVE on D**. A feature is trustworthy only while this test holds
**≥95% per tier, per session**. `parity_test` is its engine; FP3 makes it a daily automated gate
that BLOCKS a drifting feature from scoring. **If only one test in this whole system were ever
green, it must be this one.**

**Introspect — validate features against their descriptions:** the distribution + contract checks
of §5 (`make introspect`).

### 3.6 Multi-agent safety & feature evolution (no breakages, no inconsistency)
The platform assumes **several agents extend it concurrently.** The guards:
- **Unique-name registration, fail-fast.** Two groups declaring the same feature name → import
  error, not a silent shadow. No two groups may write the same store column.
- **Additive, never in-place.** You do NOT mutate a live feature's definition (that silently
  rewrites history). To change a feature, register a **new group version** (`trade_flow@v2`); old
  values stay attributable to `@v1` until the new version is re-certified and migrated. Adding a
  feature = adding a column/series; never altering an existing one.
- **Every stored value is version-stamped** with its producing group version (ties into the
  existing image-freshness/provenance gate). A parity run is always version-pinned — "the code
  that produced this value" is never ambiguous.
- **Idempotent, conflict-free writes.** Writes are per-(column, minute) idempotent upserts;
  because column names are globally unique per the registry, parallel group computation has no
  write contention and no lost updates.
- **Group-conformance gate (CI, blocking).** Before any new/changed group merges it must pass a
  standard conformance test: full metadata present, `compute` returns the declared schema,
  deterministic (R12), no look-ahead, and ≥ tolerance parity on the last settled day. This is the
  structural guard that one agent's addition can't break the platform or corrupt data.

### 3.7 Code organization (the extension surface is one directory)
```
quantlib/features/
  base.py        # FeatureGroup, FeatureSpec, InputSpec, BatchContext   (stable, review-gated)
  registry.py    # @register, uniqueness, lookup, catalog generation     (stable)
  engine.py      # input resolution, runs groups, schema validation, write (stable)
  store.py       # get_features() read API, write, retention             (stable)
  parity.py      # parity_test() + the daily gate                        (stable)
  introspect.py  # distribution & contract checks                        (stable)
  groups/        # >>> ONE FILE PER GROUP — the ONLY place agents add features <<<
    price_returns.py   trade_flow.py   quote_spread.py
    microstructure_burst.py   calendar.py   cross_sectional.py   multi_day.py
```
Infrastructure (`base/registry/engine/store/parity/introspect`) is stable and review-gated.
Routine feature work touches only `groups/` — extension without breakage by construction.

### 3.8 The interface in use (worked examples — the bar for ergonomics)
The interface must be interpretable and do useful things out of the box. These snippets are the
intended ergonomics, not pseudo-code to file away — FP0 ships them working.

**Define a group** (parameterized template, one file under `groups/`; vectorized batch compute):
```python
@register
class PriceReturnGroup(FeatureGroup):
    name, version, owner = "price_returns", "1.0.0", "modeller"
    type    = FeatureType.PRICE
    windows = [1, 5, 15, 30, 60]                      # minutes
    inputs  = [Input.minute_agg("close")]

    def declare(self) -> list[FeatureSpec]:
        return [
            FeatureSpec(
                name=f"ret_{w}m",
                description=(f"Simple close-to-close return over the trailing {w} minutes, "
                             f"point-in-time as of the minute open; spans all sessions."),
                dtype="float32", valid_range=(-1.0, 1.0), nan_policy="warmup",
            )
            for w in self.windows
        ]

    def compute(self, ctx: BatchContext) -> DataFrame:        # all (symbol, minute) at once
        close = ctx.frame("close")
        return ctx.emit({f"ret_{w}m": close / close.shift(w, by="minute") - 1.0
                         for w in self.windows})
```

**Read any feature, any timeframe — a sane, point-in-time, parity-true frame:**
```python
from quantlib.features import store

df = store.features.ret_5m(symbols=["AAPL", "MSFT"],
                           start="2026-06-10 09:30", end="2026-06-10 16:00")
#  -> symbol | minute | session | ret_5m      (sorted, float32, NaN only in warmup)

# extended hours are first-class — ask for a session explicitly:
pre = store.features.ret_5m(symbols="universe",
                            start="2026-06-10 04:00", end="2026-06-10 09:30",
                            session="premarket")
```

**Group-scoped access, exactly as sketched** (`group.features.<feature>(...)`, `.descriptions`):
```python
g = store.group("price_returns")
g.features.ret_5m(start="2026-06-10", end="2026-06-11")    # callable feature, scoped to the group
g.features.descriptions      # {"ret_1m": "Simple close-to-close ...", "ret_5m": "...", ...}
g.describe()                 # group metadata: owner, version, inputs, windows, per-feature status
```

**Discover & interpret (useful out of the box):**
```python
store.search("momentum")                 # -> ["mom_5d", "mom_20d", ...]
store.by_type(FeatureType.MICROSTRUCTURE)# -> features in that family, with status
store.catalog()                          # -> frame: name|group|description|parity%|latency|status|owner
store.describe("ret_5m")                 # one feature: full metadata + last parity + live distribution
```

**Ad-hoc: "would my feature have matched what we collected live yesterday?"**
```python
report = store.parity("price_returns")   # default = last settled day; recompute vs live-collected
report.summary()                         # per-tier, per-session match % + compared-cell counts
report.mismatches(n=10)                  # sampled disagreements to debug
```

**Introspect a feature's realized distribution against its declared contract:**
```python
store.introspect("ret_5m", start="2026-06-01", end="2026-06-11")
#  count, nan_pct, min/p50/max, n_unique — RAISES loudly if degenerate or outside valid_range
```

---

## 4. Requirements (numbered, each independently testable)

Functional:
- **R1** For every `(ticker, minute)` in an RTH session, a feature vector exists, as-of the
  minute open, using only data with timestamp `≤` the minute open (no look-ahead).
- **R2** Every feature is computable by the identical function live and over backfill.
- **R3** Raw trades and quotes are captured for the full target universe (≥10,000 symbols, or
  the provider's proven ceiling — see FP1.a).
- **R4** Per-minute aggregates include last-minute **trade frequency** and **quote frequency**
  (and burst/up-down statistics) — the sub-minute signal, summarized per minute.
- **R5** Any individual feature can be recomputed/updated in isolation without rebuilding others.
- **R6** The feature space is introspectable: list, filter by group/type, view description, and
  validate each feature's realized distribution against its declared contract.

Non-functional (the hard numbers — these are *bars*; the Manager may tighten, never loosen):
- **R7 — Latency.** At each minute boundary, the full vector (all features × full universe) is
  ready within **p99 ≤ 2.0 s** and **max ≤ 5.0 s**, measured minute-close→vector-persisted.
- **R8 — Scale.** The R7 bar holds at **≥500 features × ≥10,000 tickers** (proven jointly, FP4).
- **R9 — Parity.** Each feature's live value matches its backfilled value for **≥95%** of
  comparable `(ticker, minute)` cells per settled day, **within every liquidity tier** (§6.3).
- **R10 — Capture completeness.** Live raw-tick counts match same-day backfill counts within
  **±2% or ±2 ticks** for **≥98%** of `(symbol, minute)` cells, **within every liquidity tier**.
- **R11 — Storage.** A retention manager keeps the rolling raw-tick window as large as disk
  allows while holding **free disk ≥ 15%** at all times; permanent layers (aggregates, daily
  bars) are never evicted.
- **R12 — Determinism.** Re-running any feature over a fixed historical window yields
  bit-identical output across runs and machines.

Interface & multi-agent (the extensibility guarantees):
- **R13 — Read interface.** `get_features(names, symbols, start, end)` returns a point-in-time,
  parity-identical, sanely-typed result (defined dtype/order/NaN policy) for any feature over any
  in-retention timeframe, and RAISES on an uncertified feature or out-of-range request.
- **R14 — Group is the extension unit.** A new feature ships by adding ONE group file under
  `groups/`; no edits to `base/registry/engine/store/parity/introspect`.
- **R15 — Ad-hoc parity API + the T+1 cornerstone test.** `parity_test(target, day)` recomputes a
  feature/group from the **settled** historical API for that day and compares it cell-by-cell to
  the values the running system collected **live** that day, returning a per-feature, per-tier,
  per-session report — as both a Python call and a CLI target. Run on a settled day this IS the
  **T+1 Settled-Day Parity Test** (§3.5), the platform's cornerstone check.
- **R16 — Additive evolution.** Feature definitions are immutable in place; changes ship as new
  versioned groups; every stored value carries its producing group version.
- **R17 — Concurrency safety.** Feature-name uniqueness is enforced at registration; writes are
  per-column idempotent upserts; no two groups write the same column.
- **R18 — Fault isolation.** A failing group marks only its own features FAILING/unavailable
  (loud, owned); other groups still produce; no silent value substitution.
- **R19 — Extended hours from day one.** Capture, backfill, aggregates, and features cover the
  full **04:00–20:00 ET** day across all three sessions (premarket / regular / afterhours); every
  minute carries its `session` label; no RTH-only hardcoding in the capture or feature path.
  Completeness/parity bars (R9/R10) hold **within each session separately**.

---

## 5. Introspection (first-class — a platform deliverable, not a UI nicety)

A single command set, runnable by humans and agents, that catches nonsense automatically:
- `make feature-catalog` — regenerate `docs/FEATURES.md` + a JSON catalog from the registry:
  every feature with group, description, inputs, window, parity %, latency, status, owner.
- `make introspect` — for every feature emit `count, nan_pct, min, p50, max, n_unique` and
  **FAIL** if any feature is degenerate (`n_unique < 2`), exceeds its declared range, or breaks
  its declared `nan_pct` cap. Anti-nonsense: a feature whose realized distribution contradicts
  its description is flagged (e.g. a "frequency" feature going negative).
- `make parity DAY=<date>` — per-feature live-vs-backfill match table for a settled day, with
  per-tier breakdown and the compared-cell count.

---

## 6. GLOBAL ANTI-GAMING RULES (apply to EVERY criterion; override all local convenience)

These exist because the team is autonomous and the temptation to declare partial work "done"
is real. A criterion violating any rule below is **RED**, regardless of headline number.

1. **Evidence or it didn't happen.** Each green criterion must name the command that proved it
   AND commit the command's output artifact (a report file or dashboard snapshot) this cycle.
   Prose ("verified", "looks good") is not evidence.
2. **Percentiles, not means.** Latency/throughput gates are on **p99 and a hard max with zero
   allowed breaches**. Reporting only an average is RED. Always report p50/p99/max + breach count.
3. **Tier-stratified, no carrying.** Any per-cell metric (parity, completeness) must hold
   **separately within each liquidity tier** — Tier-1 (top 500 by ADV$), Tier-2 (501–2,000),
   Tier-3 (the tail). A liquid-name average masking a dead tail is RED. Report all three tiers.
4. **Minimum sample, or RED.** Every statistical claim states its denominator. A parity/
   completeness number computed on fewer than **the stated cell floor** (FP-specific) is RED,
   not a pass. You cannot pass parity on three cherry-picked minutes.
5. **No silent exclusions.** Any data excluded from a metric must be on a **pre-registered
   exclusion list** committed *before* the run, with a reason (e.g. the 16:00 auction minute).
   Ad-hoc exclusion discovered after seeing results is RED.
6. **Hard cases included.** The open minute (09:30), the close minute (15:59), the tradeless
   names, and the worst tier are all in-scope and counted. Excluding them to hit a number is RED.
7. **Binary, not "substantially."** A criterion is met or not. "90% of the way" = NOT MET.
   A milestone is DONE only when **every** sub-bullet is objectively green with its artifact.
8. **Test the failure, not just the success.** Where a check exists, prove it FAILS on a
   deliberately-broken fixture (a degenerate feature, an injected drift, a look-ahead leak) AND
   PASSES on clean data. A check only ever seen green is unproven.
9. **The number is the real number.** Universe size, feature count, and history depth are the
   *actual achieved* values, queried live — never an aspirational constant or a stale doc value.

---

## 7. Engineering Milestones (FP ladder)

Each milestone: a one-line goal, then exit criteria as `[ ] CRITERION — THRESHOLD — VERIFY:
<command/query> — EVIDENCE: <committed artifact>`. §6 applies throughout. The concrete pinned
numbers + the countable goal scoreboard for this ladder live in **`docs/FP_GOALS.md`** (headline:
**1,000 features × 10,000 tickers in < 2 s**, the growth curve 10→50→150→500→1,000, the
lifecycle-demo counts, and the NaN/QA/parity goals).

### FP0 — Registry, catalog, introspection, and the parity harness (current scale)
**Goal:** replace the monolithic feature function with a self-describing registry + the
certification machinery, proven on today's ~25 features BEFORE any scaling.

- [ ] **Registry exists; monolith gone.** The set of computed feature columns equals the
      registry's feature names exactly, and no feature math lives outside a registered feature.
      — 0 features defined outside the registry. — VERIFY: `pytest tests/test_registry.py::test_no_logic_outside_registry` (AST scan) + `test_columns_equal_registry`. — EVIDENCE: test run committed.
- [ ] **Every feature is fully self-describing.** 100% of registered features have non-empty
      `name, group, description, inputs, window, parity_tolerance, latency_budget, owner`.
      `description` is **≥ 40 chars and ≠ the feature name** (anti-stub). — VERIFY:
      `pytest tests/test_registry.py::test_metadata_complete`. — EVIDENCE: test output.
- [ ] **Add-a-feature = one file.** A scaffolded sample feature registers and computes with
      zero edits to any shared module. — VERIFY: `make new-feature NAME=demo_probe` then
      `make introspect` shows it; the diff touches exactly one new file. — EVIDENCE: the demo
      commit + diff stat.
- [ ] **Catalog generated and drift-gated.** `make feature-catalog` regenerates
      `docs/FEATURES.md` + JSON; CI fails if the committed catalog differs from generated.
      — VERIFY: `make feature-catalog && git diff --exit-code docs/FEATURES.md`. — EVIDENCE:
      committed `docs/FEATURES.md`.
- [ ] **Introspection green on all ported features.** All ~25 existing features ported; 0
      degenerate, 0 range violations, every `nan_pct ≤` its declared cap. — VERIFY:
      `make introspect` exits 0. — EVIDENCE: introspection report.
- [ ] **Introspection proven to bite.** A deliberately degenerate feature (constant) and an
      out-of-range feature each make `make introspect` exit non-zero on a fixture. — VERIFY:
      `pytest tests/test_introspect.py::test_catches_degenerate`. — EVIDENCE: test output.
- [ ] **Parity harness runs per-feature.** `make parity DAY=<settled day>` emits a table with
      one row per feature: per-tier match %, compared-cell count. — VERIFY: command output
      has rows == feature count, each with a cell count **≥ 50,000** (FP0 floor). — EVIDENCE:
      committed parity report for the day.
- [ ] **All ported features ≥95% parity, all tiers.** Every feature ≥95% in Tier-1/2/3
      separately on ≥1 settled day. — VERIFY: `make parity` summary all-green. — EVIDENCE: report.
- [ ] **Read API satisfies R13.** `get_features` returns a known `(symbol, minute)` range whose
      values equal a direct recompute, with the declared dtype/order/NaN contract, and RAISES on
      an unknown feature and an out-of-retention range. — VERIFY:
      `pytest tests/test_store.py::test_get_features_contract`. — EVIDENCE: test output.
- [ ] **Ad-hoc parity API satisfies R15.** `parity_test('<group>')` (default last settled day)
      returns a per-tier report, and the CLI mirror runs. — VERIFY: `make parity TARGET=<group>`
      + the Python call in a test. — EVIDENCE: committed report.
- [ ] **Group-conformance gate bites (R14/R16/R17/R18).** A deliberately broken group (undeclared
      column / look-ahead / non-deterministic / duplicate feature name / in-place mutation of a
      live feature) FAILS the conformance test; a clean group PASSES; a `@v2` registers alongside
      `@v1` with version-stamped values. — VERIFY:
      `pytest tests/test_group_conformance.py`. — EVIDENCE: test output.

### FP1 — Sub-minute capture at full-universe scale (raw ticks, no drops)
**Goal:** prove we can actually capture every trade and quote for the full universe, losslessly,
sized to disk — and discover the provider's real ceiling.

- [ ] **a. Provider ceiling proven.** Document the max symbols for which the provider streams
      full trades+quotes on our connection(s), with the **measured peak msg/s** sustained a full
      session. Target ≥10,000; if lower, the proven ceiling becomes the universe and is recorded.
      — VERIFY: subscription count log + a Prometheus `ingest_msgs_per_sec` panel screenshot of a
      full session. — EVIDENCE: the panel + a `docs/CAPTURE_CEILING.md` note with the number.
- [ ] **b. Lossless capture vs ground truth.** For a full RTH session, live raw-tick counts vs
      same-day backfill counts meet **R10 (±2%/±2 ticks for ≥98% of cells) within EVERY tier**.
      Compared cells **≥ 2,000,000** total. — VERIFY: `make capture-parity DAY=<day>` per-tier
      table. — EVIDENCE: committed report; the pre-registered exclusion list.
- [ ] **c. Writer keeps up under the open-bell burst.** Ingest backlog/lag **p99 < 2 s, max
      < 10 s**, and the dropped-message counter reads **exactly 0** across the full session
      (drops, if any, are counted and reported, never hidden). — VERIFY: Prometheus
      `ingest_lag_seconds` + `ingest_dropped_total`. — EVIDENCE: session panels.
- [ ] **d. Capture layer is logic-free.** No aggregation/feature code in the capture process
      (parity protection). — VERIFY: `pytest tests/test_capture_purity.py` (import/AST scan).
      — EVIDENCE: test output.
- [ ] **e. Retention holds the disk floor.** The rolling raw-tick window auto-evicts oldest;
      **free disk never < 15%** over a full session; permanent layers untouched. Record the
      achieved window length (days). — VERIFY: `make retention-check` (asserts floor + eviction
      on a simulated full disk) + a disk-free panel. — EVIDENCE: panel + recorded window length.
- [ ] **f. Throughput decision recorded.** A committed measurement states whether sharded
      Python met c; Rust capture is introduced **only if** it did not, with the failing number
      cited. — EVIDENCE: `docs/TECH_DEBT.md` entry with the number and the decision.
- [ ] **g. Extended hours captured & backfilled, all three sessions (R19).** Raw ticks captured
      AND same-day backfill available for **premarket (04:00–09:30 ET)** and **afterhours
      (16:00–20:00 ET)**, not only RTH; R10 capture-completeness met **within each session
      separately** — the thin sessions cannot be silently dropped, and 09:30/16:00 boundaries are
      counted. — VERIFY: `make capture-parity DAY=<day>` broken out by session. — EVIDENCE:
      per-session report + the session-labeled minute grid.

### FP2 — The point-in-time minute vector, at scale, within the latency budget
**Goal:** every minute, every ticker, the full vector ready within ≤2 s — the heart of the
system. Includes the last-minute trade/quote-frequency features (R4).

- [ ] **a. Minute-aggregate substrate populated, all sessions.** One `(ticker, minute)` row per
      universe member per minute across **04:00–20:00 ET**, each carrying its `session` label
      (tradeless minutes present, zero-filled per ratified semantics). — VERIFY:
      `SELECT count(distinct symbol) FROM minute_agg WHERE minute=<m>` equals the live universe
      size (±0 beyond pre-registered halted names) for ≥99.5% of minutes **in each session**.
      — EVIDENCE: per-session query log over a full day.
- [ ] **b. Last-minute micro-features live.** `trade_freq_1m`, `quote_freq_1m`, and ≥1
      up/down-burst feature are computed and non-degenerate. — VERIFY: `make introspect`
      lists them green; spot-check a known burst minute. — EVIDENCE: introspection report.
- [ ] **c. Latency bar met at session scale.** Minute-close→vector-ready **p99 ≤ 2.0 s, max
      ≤ 5.0 s, 0 minutes over max**, measured over ALL minutes of ≥1 full session at the
      current universe size. — VERIFY: Prometheus `vector_ready_latency_seconds` (p50/p99/max
      + breach count). — EVIDENCE: session panel.
- [ ] **d. Vector completeness.** Vector present and non-degenerate for **≥99.5%** of
      `(ticker, minute)` cells (NaN only where by-construction, within declared caps).
      — VERIFY: `make vector-completeness DAY=<day>` per-tier. — EVIDENCE: report.
- [ ] **e. Live == backfill on this session.** Re-evaluating `f(ticker, T)` over backfill for
      the same session matches the live-persisted vector at **≥95% per feature, per tier**
      (R9). — VERIFY: `make parity DAY=<session>`. — EVIDENCE: report.
- [ ] **f. No look-ahead, proven.** Over **≥10,000 random `(ticker, T)` samples**, no feature
      consumes data with timestamp `> T`; **0 violations**. The check FAILS on an injected-leak
      fixture. — VERIFY: `pytest tests/test_no_lookahead.py`. — EVIDENCE: test output.

### FP3 — Daily certified parity as a standing, enforced gate
**Goal:** make the **T+1 Settled-Day Parity Test** (§3.5) an automated daily invariant — the
guarantee that would have caught both prior failures, permanently — that *blocks* a drifting
feature from use.

- [ ] **a. Daily parity job scheduled and running.** Each settled day auto-backfills, diffs vs
      live per feature, writes a dated report to the catalog/ledger. — VERIFY: dated artifacts
      exist for **≥10 consecutive settled days**. — EVIDENCE: the 10 reports.
- [ ] **b. Status auto-updates.** A feature ≥95% over the trailing 5 settled days → CERTIFIED;
      else → FAILING with an auto-assigned owner, reflected in the catalog within 1 day.
      — VERIFY: inject a drift on a fixture day → feature flips to FAILING in the next catalog.
      — EVIDENCE: before/after catalog + the injection test.
- [ ] **c. FAILING features are BLOCKED, not warned.** Scoring/decisions exclude FAILING
      features; a test injects drift and confirms the feature is dropped from the served vector.
      — VERIFY: `pytest tests/test_parity_gate_blocks.py`. — EVIDENCE: test output.
- [ ] **d. Streak with honest denominators.** **≥10 consecutive settled days** where every
      CERTIFIED feature held ≥95% **in all tiers**, each day's per-feature compared-cell count
      **≥ 100,000** (a too-small-sample day is RED, not skipped). — VERIFY: the 10 reports'
      summary lines. — EVIDENCE: committed streak log.

### FP4 — Grow to 500 features, extensible and introspectable, at scale
**Goal:** prove the platform scales in feature COUNT with every guarantee intact, and that agents
(incl. modeller ideas) add features end-to-end.

- [ ] **a. ≥500 certified features × ≥10,000 tickers, jointly within budget.** Live count is
      `≥500` registered features over `≥10,000` tickers AND R7 latency (p99 ≤ 2 s, max ≤ 5 s, 0
      breaches) holds at that joint scale over a full session. — VERIFY:
      `curl .../api/features | jq length` ≥ 500, universe query ≥ 10,000, latency panel.
      — EVIDENCE: queries + session latency panel.
- [ ] **b. Every one certified.** All ≥500 features: full metadata, introspection-green,
      ≥95% parity all tiers (FP3 gate). 0 uncertified features in the served vector. — VERIFY:
      `make introspect` + `make parity` summaries. — EVIDENCE: reports.
- [ ] **c. Not padded with duplicates.** No feature pair with **|corr| > 0.99** across the
      panel without a committed justification; **≥8 distinct feature families** each with ≥1
      certified member (price, volume, trade-flow, quote/spread, microstructure-burst, calendar,
      cross-sectional, multi-day). — VERIFY: `make feature-correlation` + catalog family counts.
      — EVIDENCE: correlation report + catalog.
- [ ] **d. Modeller-originated features completed the loop.** **≥5 features** that originated as
      a modeller idea went idea→registry→certified→catalog. — VERIFY: catalog `owner=modeller`
      certified count ≥ 5. — EVIDENCE: catalog.
- [ ] **e. The sub-minute→multi-day thesis is testable.** ≥1 microstructure-burst feature family
      is certified and joinable to multi-day-forward labels, so the research in §9 can run
      regardless of whether edge is found. — VERIFY: a notebook/CLI joins a burst feature to a
      `fwd_1d`..`fwd_5d` label with non-null coverage ≥ a stated floor. — EVIDENCE: the join report.
- [ ] **f. CAPSTONE — the §1.3 lifecycle demonstrated REPEATEDLY (the definition of winning).**
      The full loop (code → live-collect-all-tickers → speed pass → distribution check → QA
      adversarial sign-off → NaN audit → selective parity-matched backfill → model trained on
      backfill and served live) is demonstrated end-to-end for **≥3 distinct new features/groups**,
      each with its complete evidence trail committed. — VERIFY: a `docs/LIFECYCLE_DEMOS.md` log
      linking each demo's artifacts (speed panel, introspection, QA note, NaN audit, parity report,
      model + live-serve proof). — EVIDENCE: the 3 demo trails. **If this criterion is not green,
      the platform is not yet winning, regardless of feature count.**

---

## 8. Non-Goals (explicit, so scope can't creep)
- **Not** sub-second / tick-level execution. Decisions are minute-cadence. Sub-minute data
  enters as *features* over the trailing minute, not as a low-latency trading loop.
- **Not** beating professional HFT on timing. The edge thesis is *fast-microstructure features
  predicting slow (day-to-week) moves*, where our latency is not the disadvantage.
- **Not** maximizing history depth. We deliberately trade raw-tick history depth for full
  sub-minute resolution and width (all tickers), bounded by R11.
- **Not** finding edge as a precondition for platform success. The platform is the deliverable.

## 9. How modelling / edge relates (the downstream track)
Edge/strategy work is a **parallel background track that consumes this platform.** Modellers
explore on certified features, and their primary obligation to the platform is to **propose new
certifiable features** (FP4.d) from their most promising leads. No edge claim is trusted unless
its features are certified (FP3) and its backtest passes the honesty gates. The platform never
idles on registry/scale/parity/introspection; the edge track fills the remaining capacity.

## 10. Open parameters to confirm with Ben (do not block FP0 on these)
- **Universe ceiling:** target ≥10,000; the real number is FP1.a's measured deliverable.
- **Trailing-window set:** the standard windows features may use (e.g. 1m, 5m, 15m, 30m, 60m,
  1d, 5d) — to be fixed in FP0 so features declare from a known menu.
- **Forward-label horizons** for §9 research (e.g. fwd_30m, fwd_1d, fwd_5d).

## 11. Feature inspiration catalog (seeds — not a required set)
These are SEEDS drawn from the prior automated-trading codebase, organized by family. They are
inspiration, not a contract: each must be implemented as a `FeatureGroup`, declare its inputs +
windows, and pass FP certification (parity + latency + introspection) before it counts. Windows
adapt to this platform — **sub-minute seconds** (from raw ticks), **trailing minutes** (1/5/15/
30/60m), and **multi-day** (1/3/5/10d) — and every feature spans all sessions (premarket/regular/
afterhours). Start FP0 with a handful from A–E; grow toward 500 across all families (FP4.c).

**A. Microstructure-burst (sub-minute) — the centerpiece of the sub-minute→multi-day thesis.**
`peak_trades_per_second`, `trade_rate`, `trade_rate_acceleration`, `trade_acceleration_gradient`,
`rapid_fire_ratio`, `burst_count`, `inter_arrival_mean_ms` / `_min_ms` / `_p10_ms`,
`trade_timing_entropy`, `trade_size_regime_change`, `size_acceleration`, `excitement`,
`institutional_ratio`, `odd_lot_ratio`, `round_lot_ratio`, `volume_vs_intraday_norm`. These detect
a name "taking off" within the last minute; their predictive target is day-to-week moves.

**B. Trade-flow / signed pressure.** `signed_volume`, `buy_volume` / `sell_volume`,
`tick_imbalance`, `buying_pressure`, `up_volume_ratio` / `down_volume_ratio`, `volume_delta`,
`ofi_{5,15,30}m`, `signed_vol_z`, `large_print_cnt` / `large_ratio`, `vwap_deviation`.

**C. Quote / spread / liquidity.** `quote_freq_1m`, `spread_bps`, `median_spread_bps`,
`spread_volatility`, `quote_imbalance` / `bid_ask_imbalance`, `average_bid_size` /
`average_ask_size`, book depth.

**D. Price / returns.** `simple_return_{1,5,15,30,60}m`, `log_return_*`, price level / position /
high / low over window, `gap_from_prior_close`, `gap_from_open`.

**E. Volume.** `volume_mean_*`, `volume_vs_avg`, `volume_spike`, `volume_rank`,
`dollar_volume_ratio`, `trade_intensity`, volume z-score.

**F. Volatility / range.** `volatility_realized_*`, `parkinson`, `garman_klass`, `high_low` range,
`percent` range, `spread_volatility`.

**G. Momentum quality / trend.** `price_slope`, `price_r_squared` (straightness), `trend_strength`,
`momentum_acceleration`, `consistent_direction`, `longest_streak`, `reversal_count`,
`residual_mean_abs` / `_std` / `_skew`, `clean_momentum_score`.

**H. Technical.** `rsi_*`, `macd_{line,signal,histogram,cross,divergence,slope}`,
`bb_{position,width}_*`, `{sma,ema}_distance_*`, `ma_trend`, `ma_cross`.

**I. Price–volume interaction.** `obv_slope`, `volume_price_trend`, `pv_correlation`,
`volume_lead_price`, `breakout_volume`, `pullback_volume`, `volume_surge_return`.

**J. Candlestick / shape.** pattern flags (doji, hammer, engulfing, …) over recent bars.

**K. Calendar / session.** `minute_of_day`, `day_of_week`, time-since-open, time-to-close,
`session` one-hot (premarket / regular / afterhours).

**L. Cross-sectional (computed across the universe each minute).** within-timestamp rank of any
feature, return vs universe median, sector-relative return, `{spy,qqq}_return_*` + beta-adjusted
residual return, relative-volume rank.

**M. Multi-day (natural predictors for the slow-horizon labels).** `daily_return_{1,3,5,10}d`,
overnight gap, multi-day realized vol, distance from N-day high / low, multi-day volume trend.
