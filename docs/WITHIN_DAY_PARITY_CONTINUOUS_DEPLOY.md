# Within-Day Parity — the CONTINUOUS-DEPLOYMENT fix-loop (Phase 3 design, DESIGN ONLY)

Status: **DESIGN SPEC — not activated.** Authored 2026-06-20 per Ben's sharpened Phase-3 model; **REVISED
2026-06-20 per two Ben decisions that OVERRIDE the earlier "sandbox-then-relaunch" recommendation:**
**(1) REAL-TIME DEPLOY** — *"It should be real-time deployed as soon as an agent wants it to be and has
adequate unit tests."* So the fix reaches LIVE capture the moment the agent decides (gated on adequate unit
tests + the in-scope gate), not at the next relaunch. **(2) STRICT SCOPE** — *"That agent can only make
changes within the scope of feature code it has been assigned."* The diff is confined to the ONE assigned
group; anything outside is FORBIDDEN, not merely gated. The cert-ledger SQL apply + the `dry_run=False` flip
are held by the Lead for the sequenced activation; this document designs the *process* + names the **first
buildable step**. It builds directly on the merged WDPC Phase 1 (the settled-window compare + settle-lag,
`within_day_parity.py`) and Phase 2 (the root-cause classifier `within_day_rootcause.py` + the
cert/trust-grant writer `within_day_trust.py`), and grounds every mechanism in what already exists (the
feature-vector BUS + the fc compute loop + the registry-per-minute group lookup).

The RTH-dent gate is **CLEARED** (crypto canary, 2026-06-20: 129 bounded compares concurrent with live
capture → no compute_ms spike, no missed minute, no restart) — so a bounded continuous agent is safe to run
against live capture. This doc is what that agent *does* once activated.

---

## 0. Ben's sharpened model (verbatim intent)

> Untrusted feature groups that aren't being worked on AND haven't been verified-to-working TODAY get picked
> up by the Lead and assigned to a feature subagent. The subagent watches real-time features, continuously
> re-creates the same value via the backfill API, and checks if they match. If they don't, it inspects WHY,
> makes code updates, and the cycle waits for the real-time features to update [with the fix]. MISSING
> INGREDIENT: continuous deployment — the subagent should deploy its feature-code fix very quickly/easily
> via a PR that could even be AUTO-MERGED (IF within the scope of the feature code it owns), and then STAY
> ONLINE watching the updated real-time values. This means the subagent needs a way to SUBSCRIBE to features
> in real-time, or poll the real-time stores continuously, to see whether recent features match the ones it
> re-creates via backfill.

The three missing ingredients to design: **(1) real-time subscription, (2) the fix-loop, (3) continuous
deployment into production** — plus **(4) the auto-merge-if-in-scope gate.**

---

## 1. INGREDIENT 1 — REAL-TIME SUBSCRIPTION (the comparison target + fresh per-minute input)

The subagent watches recent LIVE values for its assigned group. Two existing sources; the design uses both,
each for what it's best at.

### 1.1 The two sources

| Source | What it is | Latency | Use in the loop |
|---|---|---|---|
| **BUS** — `fv:<symbol>` Redis Streams (`quantlib/bus/consumer.py` `BusConsumer.poll_views`) | live per-minute `compute_latest` vectors, decoded BY NAME against the frame's own fingerprint (`poll_views` → `FeatureView`), ~4h retention | LOWEST (published off the hot path right after each minute's compute) | the **freshest** signal that a fix has landed live + a cheap per-minute "did live values just change?" tripwire |
| **STORE** — `store.get_features(source='stream')` (parquet) | the provisional live values fc WROTE for recent minutes | higher (written after compute) | the **authoritative cell-for-cell** live side of the parity compare (what Phase-1 already reads) |

### 1.2 The decision: STORE is the compare source-of-truth, BUS is the freshness tripwire

The Phase-1 compare already reads `store.get_features(source='stream')` vs `source='backfill'` on a settled
window and grades with `compare.cell_verdict` — that stays the **authoritative comparison** (it is the same
read the nightly sweep uses, so within-day == nightly by construction; ingredient 1 must not change *what*
"match" means). The BUS adds the missing capability Ben named — **subscribe in real-time** — as the
**tripwire**: the subagent `poll_views` on its group's symbols to detect *the minute the live values change*
(i.e. a relaunched fc started emitting the fixed compute), so it knows *when* to re-run the authoritative
store compare rather than polling blindly. The bus is gated `FP_BUS=1` (default OFF); the agent enables it
in its own watch scope only — if the bus is unavailable it degrades to **store-polling** the recent stream
partitions (the design never *requires* the bus, it *prefers* it for latency).

### 1.3 Scope + bounding (inherits the RTH-dent discipline)

- ONE group's symbols at a time; a SAMPLE (the Phase-1 default 30), never the universe.
- The bus consumer subscribes to exactly that sample's `fv:<symbol>` streams (per-symbol XREAD — it does
  not deserialize the other ~11k symbols).
- The whole watch + compare runs under the Phase-1 caps (`--cpus 1.0`, nice/ionice, guard-named) — proven
  no-dent on the crypto canary.

---

## 2. INGREDIENT 2 — THE FIX-LOOP (compare → classify → fix → re-confirm → certify)

One subagent owns ONE untrusted group. Its loop (all sandbox-local until the deploy step §3):

```
ASSIGN: Lead picks an UNTRUSTED group not being worked on + not within_day-certified TODAY (§5 ordering).
SUBSCRIBE: bus poll_views on the group's sample symbols (§1) → know when live values are fresh.
loop:
  1. COMPARE (Phase 1): store stream vs backfill on the settled window for the sample → per-feature value_rate.
     Pre-screen the §2.4 known non-bugs (FP_TICK_SYMBOLS / warm-up / capture-start) — never chase those.
  2. if clean + stable (Phase-3 stability: N consecutive clean settled-window comparisons): → CERTIFY (§ below) → DONE.
  3. if MISMATCH (real, on clean symbols): ROOT-CAUSE (Phase 2 classifier within_day_rootcause.py) →
     {incremental.py/aggregates.py | stateful.py/raw_loaders.py | compare-guard} with exemplars.
  4. FIX in the subagent's OWN WORKTREE (off origin/main; NEVER the live tree). The fix touches ONLY the
     owned group's code (the auto-merge scope, §4).
  5. RE-CONFIRM IN-SANDBOX (the key to speed — §3): re-run BOTH forms on RECENT LIVE DATA in the sandbox:
       - backfill form: compute() over the sample's recent raw tape (raw_loaders + materialize, sandbox store).
       - live form: compute_latest() over the SAME recent raw frames (the fast path the fix changed).
     Compare them with the SAME cell_verdict + tolerance. The fix is "good" iff this flips mismatch→clean
     AND leaves every OTHER group byte-identical (§4). No production touch yet.
  6. DEPLOY (§3): the in-scope, unit-tested fix is submitted to the deploy queue → the serialized applier
     HOT-SWAPS that one group's compute in live fc in REAL TIME (the moment the agent decides).
  7. CONFIRM IN PRODUCTION: the bus tripwire fires the NEXT MINUTE the swapped group emits → re-run step 1 on
     the now-fixed live stream → confirm PRODUCTION real-time == backfill (within a minute) → then CERTIFY.
```

Certification reuses Phase 2 exactly: `within_day_trust.write_certifications` (cert stamp + grant trust with
`reason='within_day_parity'`, one clean day → TRUSTED), held at `dry_run` until the Lead flips it.

**Why the in-sandbox re-confirm (step 5) is the speed unlock:** the subagent proves `compute_latest ==
compute` on recent real data in its own sandbox, deterministically, in seconds (this is literally the Phase-1
mechanism run on a recent window with the candidate code) — those are the "adequate unit tests" that gate the
real-time deploy. Production deploy (§3) is then the real-time hot-swap, confirmed live within a minute by the
bus tripwire.

---

## 3. ⭐ INGREDIENT 3 — REAL-TIME DEPLOY via SAFE PER-GROUP HOT-SWAP (Ben's overriding decision)

Ben's decision is unambiguous: the fix **real-time deploys the moment the agent decides, gated on adequate
unit tests** — not at the next relaunch. The earlier doc rejected hot-reload because a BROAD reload would
mutate shared capture state. Ben's version is **NARROW** (one untrusted group's compute, fingerprint
unchanged), and that narrowness is exactly what makes it tractable. The fc architecture is **already
hot-swap-ready** (grounded in the live compute loop, see §3.1).

### 3.1 The mechanism — re-import ONE group's module between minutes

The live compute loop (`quantlib/features/capture.py:369`, `process_bars`) does, every minute:

```python
selected = [g for g in runnable(frames) if g.name not in exclude_groups ...]   # runnable() → REGISTRY.groups()
out = group.compute_latest(ctx)                                                 # on the registry instance
```

The decisive fact: **the group list is re-fetched FRESH from `REGISTRY` each minute** (not cached in the
engine), and groups are looked up **by `name` (a string)**. So replacing one group's instance in
`REGISTRY._groups[name]` makes the **next minute** call the new compute — no engine coordination needed.

The hot-swap is therefore: **re-import the assigned group's module** (re-executes its `@register` decorator →
`REGISTRY.register(new_cls)` instantiates a fresh instance and overwrites `REGISTRY._groups[name]`), guarded
so it happens **between minutes** (at a minute boundary, never mid-compute). The SHARED capture state
(`CaptureState.ring` raw-bar buffer, `CaptureState.engines` incremental dict, `CaptureState.bus_hook`
publisher + its schema, the store writer) lives in `CaptureState` — **completely separate from the per-group
compute objects** — and is **NOT touched** by the swap. The feature SET + fingerprint are unchanged (same
`group:name:version`), so the publisher/codec/schema are untouched; only that one group's compute LOGIC
swaps.

### 3.2 Safe-by-construction conditions (all required, fail-closed)

1. **FINGERPRINT UNCHANGED** — no shape/schema change (same `group:name:version`) → publisher/codec/schema
   untouched, no coordinated deploy. (The bus fingerprint hashes `group:name:version` lines; a same-version
   compute correction is fingerprint-neutral by construction.)
2. **UNTRUSTED feature only** — never consumed by a live strategy (§6), so the in-flight value change can't
   affect a trade.
3. **Value-change confined to that ONE group** — byte-eq on every other group (§4 condition 4).
4. **ADEQUATE UNIT TESTS** — proven in-sandbox: `compute_latest == compute` flips MISMATCH→CLEAN on recent
   live data, AND byte-eq elsewhere (§2 step 5 = §4 conditions 3+4). These ARE the "adequate unit tests"
   gating the deploy.
5. **Swap at a minute boundary** — the applier swaps between minutes (never mid-`process_bars`), so no minute
   sees a half-old/half-new compute.

### 3.3 ⭐ The carried-state case — which group KINDS are real-time-swappable

A swap replaces the group INSTANCE, so any state held **inside the instance** (or keyed to it) is lost. The
grounding (incremental.py / stateful.py / base.py SessionCache) gives a precise classification:

| Group kind | Carries cross-minute state? | Real-time swappable? | What the applier does |
|---|---|---|---|
| **BATCH ReductionGroup** (FP_INCREMENTAL OFF — the live default today) | NO — `compute_latest` re-derives from the shared raw ring buffer each minute | ✅ **SWAP DIRECTLY** any minute | re-import → next minute uses new compute; zero reseed |
| **Declarative / candlestick / calendar / reference / cross-sectional** (stateless recompute) | NO | ✅ **SWAP DIRECTLY** | as above |
| **Class-A SessionCache groups** (sector_beta etc. — per-instance `self._session_cache`) | YES, but cheap: a per-session-invariant cached frame | ✅ **SWAP DIRECTLY** (with ~1-min warm-up) | re-import → cache misses next minute → auto-recomputes from the daily snapshot (sub-minute); a transient first-minute cost, then identical |
| **Incremental ReductionGroup** (FP_INCREMENTAL ON — engine `WindowedSumState` in `CaptureState.engines`) | YES — running per-(window,symbol,col) sums | ⚠️ **SWAP + RESEED** | re-import, then `IncrementalEngine.seed(ring.materialize())` rebuilds the running sums from the current buffer → next minute folds from the reseeded state (the existing seed abstraction; `seed(H);fold(m) == seed(H+m)` by the parity guarantee) |
| **StatefulGroup** (EMA / cumulative / lag — accumulators inside the instance) | YES — recursive accumulators (EMA history, session-cumulative, lag ring) | ⚠️ **SWAP + RESEED** | re-import, then re-seed the new instance's accumulators by replaying the recent buffer (`StatefulGroup.compute()` fold == full recompute); without a reseed the EMA rebases / cumulative resets / lag empties = a warm-up seam |

**The honest bottom line on kinds:**
- **Real-time-swappable DIRECTLY (no reseed):** batch reduction groups (the live default — FP_INCREMENTAL is
  UNARMED in prod today), all stateless declarative kinds, and Class-A cache groups (sub-minute warm-up).
  **This is the majority of the live feature set today**, so most fixes deploy real-time with zero reseed.
- **Swappable WITH a reseed:** incremental-armed reduction groups + stateful groups. The reseed abstraction
  EXISTS (`IncrementalEngine.seed`, the stateful fold==recompute guarantee), so these are swappable —
  but the applier must call the group's reseed after the re-import, and the reseed must be part of the
  hot-swap path (the key NEW infra to build + test for these kinds).
- **Irreducible / relaunch-only (HONEST):** if a fix to a stateful/incremental group ALSO needs a reseed
  whose correctness can't be cheaply proven mid-session (e.g. a session-cumulative whose semantics changed,
  not just a per-minute bug), OR a fix that for any reason can't be made fingerprint-neutral, it is **NOT
  real-time-swappable** → it falls back to a **fast off-RTH relaunch for ONLY that group's change**, Lead-
  sequenced. The applier must DETECT this (the reseed-proof fails or the fingerprint moves) and **escalate
  to the Lead rather than swap**. We do NOT pretend every kind is equally safe.

### 3.4 Production-confirm latency

Real-time: the swap takes effect the **next minute** the group computes; the bus tripwire (§1) confirms the
live stream now matches backfill **within a minute**. Certification collapses toward ONE stage —
PRODUCTION-CERTIFIED as soon as the live tripwire confirms the swap (the earlier two-stage SANDBOX→PRODUCTION
split is now seconds-to-a-minute apart, so the cert stamp can write `certified` directly once the live
confirm lands; `fix_pending` remains only for the brief in-flight window or an escalated relaunch-only case).

---

## 4. INGREDIENT 4 — THE SCOPE-GUARD on the auto-deploy queue (the precise safe rule)

The scope guard is the gate that decides a fix may auto-deploy (real-time hot-swap, §3) vs must escalate to
the Lead. A fix AUTO-DEPLOYS iff **ALL** of these hold (checked mechanically, fail-closed):

1. **OWNED-SCOPE ONLY (single-group-isolated)** — the diff touches ONLY the assigned group's feature code
   (its `groups/<group>.py` + any group-PRIVATE helper it owns). A diff touching SHARED code (a reduction
   kernel two groups use, `incremental.py`, `aggregates.py`, `compare.py`, anything outside the owned group's
   file set) → FORBIDDEN for auto-deploy → human review. (Mechanical: `git diff --name-only` ⊆ the group's
   declared private file set; per Ben's strict-scope decision this is a HARD boundary, not just a gate.)
2. **FINGERPRINT UNCHANGED** — `BusSchema.from_registry()` fingerprint byte-identical before/after (no version
   bump, no add/remove/rename/reorder) → publisher/codec/schema untouched, hot-swap safe.
3. **PARITY GATE FLIPS** — the fix turns the feature's verdict MISMATCH→CLEAN (value_rate ≥ `min_pass_rate`),
   proven in-sandbox (`compute_latest == compute` on recent live data).
4. **BYTE-EQ ELSEWHERE** — every OTHER group's output is byte-identical before/after on the same sample (the
   fix is surgical; the hot-swap can't perturb a TRUSTED feature). (Mechanical: re-materialize all groups
   pre/post, assert equality outside the owned group.)
5. **VALUE-CHANGE ONLY ON THE UNTRUSTED FEATURE** — the only cells that change are the assigned UNTRUSTED
   feature's (not traded → safe, §6). A TRUSTED feature's values must NOT move → else escalate.
6. **ADEQUATE UNIT TESTS + QA GREEN** (Ben's explicit deploy gate) — the group's unit tests (incl. the new
   regression that fails-without/passes-with the fix) + the parity suite pass; ruff/black/isort/mypy clean.
   This is the condition Ben named as the real-time-deploy gate; it is REQUIRED, not optional.
7. **HOT-SWAP-SAFE KIND** (§3.3) — the group is directly-swappable, OR swappable-with-a-reseed whose reseed
   proof passes. An irreducible/relaunch-only kind → escalate (do NOT auto-deploy).

If ANY condition fails → **NOT auto-deployed**; escalates to the Lead/human for coordinated review (the
existing worktree→PR path). The dangerous classes — SHARED-code changes (a kernel two groups use),
fingerprint-changing changes, anything perturbing a TRUSTED feature — **never race through the auto-queue**;
they are out of scope by construction and go to a human.

The check composes the **existing** `ops/bus_compat_gate.py` (fingerprint/contract safety) with the WDPC
in-sandbox parity proof + the byte-eq-elsewhere check + the kind classifier (§3.3). Nothing new conceptually
beyond the kind check — it's the deploy-safety we already enforce, narrowed to "owned untrusted group,
fp-neutral, parity-flipping, hot-swap-safe."

---

## 5. ⭐ THE MULTI-AGENT CONTINUOUS-DEPLOYMENT SYSTEM (kept as simple as possible)

Ben: this is a multi-agent flow ("some type of queuing thing") that must stay **as simple as possible**. The
design leans on ONE simplifier that makes the whole conflict class vanish, and adds the minimum on top.

### 5.1 ⭐ DISJOINT ASSIGNMENT = the conflict-preventer (THE simplifier)

Each subagent owns **EXACTLY ONE** feature group via an **assignment / lock table**. Because every agent's
scope is its own group's private code (§4 condition 1, a HARD boundary), **two agents never touch the same
file** — their diffs are disjoint by construction. **The entire git/merge-conflict class simply does not
exist.** There is no multi-writer merge logic to build, no conflict resolution, no rebase races — disjoint
scopes remove the need. This is THE reason the system stays simple; everything below leans on it.

```sql
-- db/init/14_wdpc_assignment.sql (proposed) — the lock table that makes scopes disjoint
CREATE TABLE IF NOT EXISTS within_day_assignment (
  group_name   text        NOT NULL,           -- the ONE owned group
  agent_id     text        NOT NULL,           -- the subagent that owns it
  claimed_at   timestamptz NOT NULL DEFAULT now(),
  heartbeat_at timestamptz NOT NULL DEFAULT now(),  -- liveness; a dead agent's lock times out (§5.4)
  status       text        NOT NULL DEFAULT 'active',  -- 'active' | 'released' | 'timed_out'
  PRIMARY KEY (group_name)                      -- ONE owner per group, enforced by the DB
);
```

The PRIMARY KEY on `group_name` is the lock: a second agent cannot claim an owned group. The Lead (or the
ordering query §6) assigns by INSERTing a row; the agent RELEASEs on done; a stale lock times out (§5.4).

### 5.2 ONE FIFO DEPLOY QUEUE + ONE SERIALIZED APPLIER (the only added machinery)

Agents do NOT deploy directly. A tested + in-scope fix is **submitted to a single FIFO deploy queue**; a
**single deployer process dequeues and applies ONE AT A TIME**:

```
agent: fix proven (§2) + passes the scope-guard (§4) → ENQUEUE {group, commit, agent_id} to the deploy queue
deployer (single, serialized):
  loop:
    job = dequeue (FIFO)
    re-run the scope-guard on the job (defence-in-depth; fail-closed)   → fail → escalate to Lead, skip
    AUTO-MERGE the in-scope commit to main (disjoint scope → never conflicts)
    HOT-SWAP that one group in live fc (§3: re-import + reseed-if-needed, at a minute boundary)
    CONFIRM via the live bus tripwire next minute (§1) → live == backfill?
       yes → write cert 'certified' + grant trust (§2)
       no  → ROLL BACK the swap for that one group (re-import the prior commit) + flag the Lead (§5.4)
    next job
```

**Serialization is the whole safety story for deploys:** one applier, one swap at a time, means **no
deploy/reload race** — two hot-swaps never overlap, the registry is mutated by one writer, and each swap is
confirmed (or rolled back) before the next begins. Because assignment is disjoint, the queue needs **no
merge logic** — it is a plain FIFO of independent jobs. This is the minimal machinery: one lock table, one
queue, one applier. Resist adding orchestration (multi-writer merge, dependency graphs, parallel appliers) —
disjoint assignment makes all of it unnecessary.

The queue can be a DB table (`within_day_deploy_queue`, FIFO by `enqueued_at`, status open→applied/failed) —
no new infra, same idempotent-DB pattern as the cert ledger; the single deployer is one Lead-sequenced loop.

### 5.3 Why this is conflict-free by construction (the one-paragraph proof)

Disjoint single-group assignment → no two diffs touch the same code → **no git conflicts**. Single serialized
applier → no two hot-swaps overlap → **no deploy/reload races**. Scope-guard → only fingerprint-neutral,
single-group, untrusted, unit-tested fixes enter the queue → **the dangerous classes (shared-kernel changes,
fingerprint changes, trusted-feature perturbation) never race through; they escalate to a human.** Those
three properties together are the entire correctness argument — nothing more is needed.

### 5.4 HONEST remaining challenges (the cases the simplicity does NOT magic away)

1. **Two agents whose groups SHARE a kernel.** Agent A owns group X, agent B owns group Y, but the real fix
   is in a reduction kernel BOTH use (`incremental.py`, a shared `aggregates` helper). Neither agent may
   touch it (§4 condition 1 forbids out-of-scope). → The shared change is **out of scope for both** →
   ESCALATES to the Lead as a coordinated change (a human edits the shared kernel, re-validates all affected
   groups, deploys via the normal coordinated path). The auto-system correctly REFUSES it rather than letting
   one agent edit shared code. This is the right outcome, but it means **not every parity bug is auto-fixable
   by a single agent** — the shared-substrate bugs are inherently human-coordinated. Honest + by design.
2. **Post-swap tripwire FAILURE.** The hot-swap lands but the live tripwire shows live still ≠ backfill (the
   fix was wrong, or a reseed was needed and wasn't applied, or a live-only condition the sandbox missed). →
   The deployer **ROLLS BACK that one group's swap** (re-import the prior commit — symmetric to the swap,
   same single-group hot-swap path) so production returns to the last-known state, marks the cert
   `fix_pending`/`defected`, and flags the Lead. The untrusted-only scope means this rollback risks no trade.
   This is why the applier is serialized + confirms each swap before the next: a bad swap is contained to one
   group and reverted before anything else deploys.
3. **Queue starvation / ordering.** A flood of fixes, or one group repeatedly failing + re-enqueuing, could
   starve others. → FIFO by `enqueued_at` + a per-group re-enqueue backoff (a group that fails its tripwire N
   times stops auto-retrying and escalates) + the ordering (§6) feeding assignment, not the deploy queue.
   Keep it FIFO-simple; only add fairness if starvation is observed.
4. **Assignment lock lifecycle (claim / release / timeout).** Claim = INSERT (PK blocks a double-claim).
   Release = on certify/done. Timeout = a `heartbeat_at` the agent updates each cycle; a lock whose heartbeat
   is older than a threshold is reclaimable (`status='timed_out'`) so a dead agent doesn't hold a group
   forever. The Lead can force-release. Minimal: a heartbeat column + a reclaim query, no leader election.

---

## 6. ORDERING — which untrusted group the Lead assigns next

Reuses the Phase-3 ordering already specced (docs/WITHIN_DAY_PARITY_CERTIFICATION.md §4), refined to Ben's
"not being worked on AND not verified-today" predicate:

1. **JUST-REFACTORED first** — a group whose version was just bumped by a latency refactor (FP_INCREMENTAL /
   Rust kernel) and is NON_TRUSTED for the new version. The WDPC is how it re-earns trust same-day.
2. **UNTRUSTED-not-worked-not-certified-today** — NON_TRUSTED for the live version, no open subagent
   assignment, no `within_day_parity_cert` row for today. (Mechanical: `feature_trust.trust_state` +
   today's cert ledger + an assignment lock.)
3. **TRUSTED re-affirm** — a light pass over trusted groups (lowest priority).

The queue is DB-derived each cycle (trust state + cert ledger + assignment lock) → stateless across agent
wakes, no lost in-memory state. An **assignment lock** (a row the Lead writes when it assigns a group to a
subagent) prevents two subagents working the same group ("not being worked on").

---

## 7. WHY UNTRUSTED-SCOPE MAKES THIS SAFE (the foundation of the whole design)

An untrusted feature is **NOT consumed by any live strategy** (strategies gate on `trust_state='TRUSTED'`;
the trusted-features view is the selection surface). So an in-flight parity fix that *changes the untrusted
feature's live values* **cannot affect any trade** — there is no trading position riding on a value that's
still being corrected. This is what makes auto-merge + fast iteration safe: we are tightening an
unused-for-trading signal toward its canonical form, in public view, with no live-money exposure. The moment
it's certified + trusted, it becomes eligible for trading — but by then it has *proven* live==backfill. The
risk that auto-merge guards against is therefore not "bad trades" (impossible on an untrusted feature) but
"a fix that silently perturbs a TRUSTED feature or the fingerprint" — which conditions 2/4/5 of §4 catch and
route to a human.

---

## 8. FIRST BUILDABLE STEP (gate-read before building)

The smallest piece that de-risks the whole loop and is independently useful: **the BUS-SUBSCRIPTION WATCH +
the in-sandbox re-confirm harness** — ingredient 1 + the §2-step-5 / §4-condition-3 proof — WITHOUT the
auto-merge or the live deploy. Concretely:

- `quantlib/features/within_day_watch.py`: a bounded `poll_views` subscriber over a group's sample symbols
  that detects "live values for this group changed at minute T" (the tripwire) and, on each fresh window,
  invokes the Phase-1 compare. Read-only; the bus-freshness half of ingredient 1.
- `quantlib/features/within_day_reconfirm.py`: the in-sandbox proof — given a group + a candidate code tree
  (a worktree) + a recent raw window for the sample, run BOTH `compute()` (backfill form) and
  `compute_latest()` (live form) over the SAME recent raw frames and assert `cell_verdict` clean + byte-eq
  on other groups. This is §2-step-5 / §4-condition-3 + condition-4 as a callable, testable function.
- Tests + a bounded ops driver (the Phase-1 cap pattern). Run it as a manual single-group spot-check first
  (like Phase 1 was), measured for resource use, before any continuous loop or auto-merge.

This proves the two mechanisms the whole continuous loop rests on (real-time freshness detection + the fast
in-sandbox fix-proof) with ZERO production-deploy risk and ZERO auto-deploy — exactly the Phase-by-phase
discipline that got Phases 1-2 in cleanly.

**The KEY NEW INFRA (the next, separately gate-read build) = the per-group hot-swap mechanism in fc (§3).**
This is the genuinely new piece (everything else reuses existing primitives). It is a SMALL, testable unit:
a `hot_swap_group(name)` that, between minutes, re-imports the group's module → re-registers (overwriting
`REGISTRY._groups[name]`) → reseeds its state if the kind requires it (§3.3) → returns the new instance, all
guarded to a minute boundary and asserted fingerprint-unchanged. It is testable OFFLINE first (swap a group
in a non-live engine + a captured buffer, assert the next minute's compute uses the new logic + the reseed
yields parity) before it ever touches live fc — and its live activation rides the same RTH-dent / Lead-
sequenced discipline. Then the scope-guard (§4) + the one-queue-one-applier CD system (§5) wire on top.

---

## 9. Open questions for the gate-read

1. **Auto-deploy authority** — the deployer auto-merges to `main` + hot-swaps live with no human in the loop.
   Even fully in-scope, does Ben want SILENT auto-deploy, or auto-deploy-with-notify (apply + post to the
   Lead, who can roll back)? Recommend auto-deploy-with-notify for the first N fixes, then silent once
   trusted — the rollback path (§5.4) makes either safe.
2. **Hot-swap-reseed correctness for stateful/incremental kinds (§3.3)** — for the swap-with-reseed kinds, is
   the reseed-from-buffer proof (`seed(H);fold(m)==recompute`) sufficient to auto-deploy them, or should the
   FIRST cut of real-time hot-swap be restricted to the DIRECTLY-swappable batch/stateless/cache kinds (the
   majority today, FP_INCREMENTAL unarmed) and the reseed kinds stay relaunch-only until the reseed path is
   itself proven live? Recommend: ship direct-swap kinds first; gate reseed-swap behind its own proof.
3. **The bus `FP_BUS=1` enablement** — the bus emit is gated OFF in prod today. Enabling it for the watch
   tripwire is itself a (tiny, off-hot-path, proven-safe) change to live fc — it rides the same RTH-dent /
   Lead-sequenced discipline as the agent activation. (If the bus stays off, the tripwire degrades to
   store-polling recent stream partitions — slightly higher latency, still real-time-ish.)
4. **Assignment lock granularity + lifecycle** — confirm the `within_day_assignment` PK is `group_name` (one
   owner per group) + the heartbeat-timeout reclaim threshold (§5.4); and whether the Lead pre-assigns or the
   ordering query (§6) auto-claims the next group for an idle agent.
5. **Stability N** — confirm N (≈2-3 clean windows) before a real-time-confirmed swap writes `certified` +
   grants trust (the production tripwire confirm replaces the old two-stage split, which now collapses to
   seconds-to-a-minute).

---

## 10. Summary — the design in one paragraph

Each subagent owns EXACTLY ONE untrusted group via an assignment LOCK (`within_day_assignment`, PK on
`group_name`) — so scopes are **disjoint by construction and the entire git-conflict class vanishes** (THE
simplifier). The agent SUBSCRIBES to that group's live values via the bus (`poll_views`, the freshness
tripwire) and authoritatively COMPARES live-stream vs backfill on the settled window (Phase-1, same tolerance
as nightly). On a real mismatch it ROOT-CAUSES (Phase-2 classifier), FIXES in its own worktree (touching ONLY
its owned group — anything shared is FORBIDDEN → escalates to a human), and PROVES the fix in **seconds**
in-sandbox (`compute_latest == compute` on recent live data = the "adequate unit tests"). It submits the
tested + in-scope fix to a **single FIFO deploy queue**; a **single serialized deployer** dequeues one at a
time → re-checks the scope-guard (fp-unchanged, single-group, untrusted-only, hot-swap-safe kind) → auto-
merges (disjoint scope → never conflicts) → **HOT-SWAPS that one group's compute in live fc in REAL TIME**
(re-import the module between minutes, overwriting `REGISTRY._groups[name]`; reseed its state if the kind
carries any; shared capture state — ring buffer, publisher, schema — untouched because the fingerprint is
unchanged) → the bus tripwire confirms live==backfill **within a minute** → cert `certified` + trust granted;
on a tripwire failure it ROLLS BACK that one group's swap + flags the Lead. Safe because an untrusted feature
is never traded (an in-flight value change can't affect a position), the scope-guard blocks the only real
risks (shared-code/fingerprint/trusted-feature changes), disjoint assignment removes git conflicts, and
serialization removes deploy races. **Real-time deploy mechanism: per-group hot-swap (Ben's decision) —
production-confirmed within a minute, with an honest relaunch-only fallback for the irreducible
stateful-reseed cases the applier can't safely swap mid-session.**
