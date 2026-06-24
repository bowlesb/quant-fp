# The FEATURE-WORKER FLEET — an autonomous pool that drives every group through the lifecycle

Status: **BUILT (dry-run-default), activation Lead/Ben-gated.** Authored 2026-06-24 per Ben's ask: *">=5
agents continuously monitoring different features, picking them off a QUEUE, and working each to its NEXT
lifecycle phase."*

The key fact: **the infrastructure already exists.** This is a thin autonomous-pool layer over the merged
within-day parity certification (WDPC) machinery — a **priority queue** (`feature_queue.py`) and a **worker
orchestrator** (`feature_worker.py`) that wires the existing primitives into a self-driving fleet. Nothing
about the feature math changes; this is orchestration (fp-neutral).

---

## 1. The mapping — fleet concepts to EXISTING modules

| Fleet concept | What it actually is | Module |
|---|---|---|
| THE QUEUE | the feature-GROUPS that still need work, ordered by lifecycle state, DERIVED (not a new table) from the tables the lifecycle already writes | `quantlib/features/feature_queue.py` (new) |
| THE CLAIM / DEQUEUE | the disjoint one-owner-per-group assignment lock (PK `group_name`, heartbeat, stale-reclaim) | `quantlib/features/within_day_assignment.py` |
| ADVANCE ONE PHASE (clean group) | claim → monitor settled window live==backfill to a streak → certify + trust grant → version reset → release | `quantlib/features/within_day_run.py` (`run_group_lifecycle`) → `within_day_monitor.py`, `within_day_trust.py`, `within_day_version.py` |
| ADVANCE ONE PHASE (divergent group) | read the OPEN defect exemplars → classify the likely code path (a triage report, NOT an auto-fix) | `quantlib/features/within_day_rootcause.py` (`classify_feature`) |
| THE ZERO-GAP DEPLOY of a fix | the FIFO deploy queue + the serialized applier + the Lead-gated hot-swap | `within_day_deploy_queue.py`, `within_day_applier.py`, `hot_swap.py` / `within_day_live_wiring.py` (`FP_WDPC_LIVE_SWAP`) |
| THE WORKER (one runnable) | pick next → advance → release → loop | `quantlib/features/feature_worker.py` (new) |
| THE POOL maintainer | a cheap cron that keeps >=N workers alive | `ops/feature_worker_fleet.sh` (new) |
| OBSERVABILITY | the assignment table = who's working what; the Lifecycle dashboard tab rolls it up | `services/dashboard/lifecycle_state.py` (existing read side) |

So the only NEW code is: the priority-queue ordering, the worker loop that wraps the existing
`run_group_lifecycle`, and the respawn cron. Everything load-bearing — the lock, the monitor, the cert/trust
grant, the deploy queue, the hot-swap — is the merged WDPC system.

---

## 2. The lifecycle (the phases a worker advances a group through)

```
   UNVERIFIED ──monitor→certify──> CERTIFIED(within-day) ──nightly trust──> TRUSTED
       ▲                                                                       │
       │                                                                       │ (done — off the queue)
   DIVERGENT (live != backfill) ──root-cause→fix→deploy──> re-monitor ─────────┘
```

* **UNVERIFIED** — registered, never certified, no live owner. A worker runs the monitor to certify it.
* **CERTIFIED_PENDING_TRUST** — passed the within-day compare (a `certified` row in `within_day_parity_cert`)
  but not all its features hold the permanent binary `TRUSTED` grant yet (the nightly sweep grants that).
* **DIVERGENT** — an OPEN `feature_parity_defect` row: live != backfill on a clean day. The most urgent: the
  live fast path is wrong. The worker triages it (read-only); the fix is a human/agent worktree→PR→Lead.
* **TRUSTED** — every feature of the group holds `trust_state='TRUSTED'`. Done; drops off the queue.

This is exactly the staged progression `services/dashboard/lifecycle_state.py` already computes per group
(`UNVERIFIED → MONITORING → CERTIFIED → TRUSTED`) — the fleet is the *write side* that drives a group from
one stage to the next, and that dashboard tab is its live view.

---

## 3. The priority queue (`feature_queue.py`)

The queue is **not a new table**. It is the registry's groups, MINUS the ones that are done or already being
worked, ordered by lifecycle phase. The ordering is a **pure function** (`order_groups`) over four reads, so
the priority logic is unit-tested with no database; `queue_snapshot` / `next_group` are the thin DB shell.

**Four reads (one small indexed query each):**

1. `feature_parity_defect` — OPEN/investigating rows ⇒ the group is **DIVERGENT** (the highest priority).
2. `within_day_parity_cert` — a group's latest cert_day; `certified` iff EVERY stamp that day is certified.
3. `feature_trust` — `trust_state='TRUSTED'` feature count per group ⇒ a fully-trusted group is excluded.
4. `within_day_assignment` — `active` locks; a **live** (non-stale heartbeat) lock excludes the group (it is
   being worked); a **stale** lock re-offers the group (dead-agent reclaim).

**Priority (lowest value picked first):**

| Priority | Phase | Condition |
|---|---|---|
| 0 | `DIVERGENT` | >=1 OPEN parity defect |
| 1 | `UNVERIFIED` | never certified, free to claim |
| 2 | `MONITORING_STALE` | a dead agent left an `active` lock (reclaim + re-monitor) |
| 3 | `CERTIFIED_PENDING_TRUST` | certified within-day, awaiting the nightly trust grant |

Within a phase, groups sort alphabetically for stable, reproducible output. **DIVERGENT first** is Ben's
ordering: fix what's actively wrong before certifying what's merely unverified.

**Conflict-freedom is structural.** A group with a live lock is never even *offered* to a free worker, so two
workers rarely race; and if they do (the row is taken between the queue read and the claim), the assignment
lock's PK INSERT lets exactly one win — the loser skips to the next group. No coordinator, no central
scheduler: the lock IS the mutual exclusion.

---

## 4. The worker (`feature_worker.py`) — one runnable, spawned N times

A single worker's loop (`run_worker`):

```
while work remains:
    item = feature_queue.next_group()          # highest-priority claimable group (DIVERGENT first)
    if item is None: idle-sleep, re-poll        # (or exit, with --once / --max-iterations)
    advance_group(item):
        if DIVERGENT:
            triage_divergent_group()            # read OPEN defects → classify_feature() → root-cause report
            # NO code edited, NO lock taken. Hand off to a fixing agent (worktree→PR→Lead).
        else:
            within_day_run.run_group_lifecycle()  # claim → monitor→certify → version reset → release
```

* **CLEAN phases** (UNVERIFIED / MONITORING_STALE / CERTIFIED_PENDING_TRUST) delegate entirely to the
  existing `run_group_lifecycle`, which already does claim → monitor-to-certify → on-certify version reset →
  deploy-queue peek → release. A MONITORING_STALE group's dead lock is reclaimed by the claim's timeout
  branch (`within_day_assignment._CLAIM`). The worker adds nothing to the certify path — it just selects the
  group and reports the outcome.

* **DIVERGENT** is the one branch the worker handles itself, and it is **read-only**: it loads the OPEN
  `feature_parity_defect` exemplars and runs `within_day_rootcause.classify_feature` to produce a triage —
  which code path (`incremental.py` / `stateful.py` / `raw_loaders.py` / `materialize.py`) likely diverged.
  The WDPC **never auto-pushes code**, so the worker does NOT edit code, take the lock, or enqueue a deploy.
  The triage is the actionable hand-off for a fixing agent, whose fix routes a worktree→PR through the Lead;
  the fix's deploy is the FIFO `within_day_deploy_queue` + the Lead-gated hot-swap.

**`--once`** advances exactly one group and exits — the cron-respawn unit (the cron is the loop, so a hung
monitor can never wedge a slot). The default loops until the queue empties.

---

## 5. Maintaining the pool (`ops/feature_worker_fleet.sh`)

This box has no systemd-user, so the pool is kept alive by a **cheap cron** running the guard every few
minutes — the SAME idempotent pattern as `ops/ci_daemon_guard.sh`. The guard counts live workers and
launches only the deficit to reach the target (default 5), each detached via `setsid` + `nohup` so it
survives the guard exiting.

```bash
ops/feature_worker_fleet.sh            # ensure >=N workers alive (N=FLEET_SIZE, default 5)
ops/feature_worker_fleet.sh --status   # how many alive, change nothing
ops/feature_worker_fleet.sh --stop     # stop the whole fleet
```

**Recommended cron** (add to `ops/install_crons.sh`; every 3 minutes keeps >=5 alive):

```cron
*/3 * * * * /home/ben/.fleet-repo/ops/feature_worker_fleet.sh >> ~/.quant-ops/feature_worker_fleet.log 2>&1
```

The guard runs workers from a **dedicated, always-current checkout** (`/home/ben/.fleet-repo`, fetched +
`reset --hard origin/main` each cycle), decoupled from the pinned fc bind-mount tree — the fleet is
read-mostly orchestration and must never depend on or mutate the live fc tree.

**Lead/cron spawn pattern (alternative to the shell cron).** A Lead can spawn N worker agents directly, each
with a distinct `--agent-id`, instead of (or alongside) the cron — the assignment lock makes them
conflict-free regardless of how they are launched. The shell guard is the unattended path; agent-spawning is
the interactive path.

---

## 6. Safety — every activation is gated

* **Dry-run by default, end to end.** Every worker runs WITHOUT `--write-lock`/`--write-cert`, so it reads
  the queue and logs the intended claim/advance but writes NOTHING to the DB. `feature_queue.queue_snapshot`
  short-circuits to an empty queue in dry-run (no DB read at all). The whole fleet is exercisable offline.
* **Arming is one click.** `FLEET_WRITE=1` (the guard passes `--write-lock --write-cert`) flips the workers
  to take the real assignment lock and write certs/trust grants. That is the Lead's/Ben's gated step.
* **The worker never touches the live pipeline.** No live-tree edit, no fc restart, no hot-swap apply, no
  deploy enqueue. A DIVERGENT fix is triaged (read-only) and handed off; the live hot-swap stays behind
  `FP_WDPC_LIVE_SWAP` (Lead-gated, `within_day_live_wiring`).
* **fp-neutral.** No feature math changes — this is queue + loop + cron orchestration only.

---

## 7. Observability

* **Who is working what:** `within_day_assignment` (every `active` row = a worker holding a group). The
  worker's `agent_id` is `fworker-<host>-<uuid8>`, so the row identifies the box + the specific worker.
* **The lifecycle roll-up:** the dashboard's Lifecycle tab (`services/dashboard/lifecycle_state.py` →
  `/api/lifecycle-state`) already shows each group's furthest stage + the active owners — the fleet's live
  view, no new dashboard code needed.
* **The fleet's own log:** `~/.quant-ops/feature_worker.log` (worker iterations) and
  `~/.quant-ops/feature_worker_fleet.log` (the guard's respawn decisions).

---

## 8. Files

| File | Role |
|---|---|
| `quantlib/features/feature_queue.py` | the priority-queue ordering (`order_groups` pure; `next_group`/`queue_snapshot` DB shell) |
| `quantlib/features/feature_worker.py` | one feature-worker (`run_worker`/`advance_group`/`triage_divergent_group`) + CLI |
| `ops/feature_worker_fleet.sh` | the respawn guard (keep >=N alive; `--status`/`--stop`; dry-run default) |
| `tests/test_feature_queue.py` | the ordering: DIVERGENT-first, trusted-excluded, live-lock-excluded, stale re-offered |
| `tests/test_feature_worker.py` | the loop: clean→lifecycle, divergent→triage (never lifecycle), claim-race skip, `--once` |
```
