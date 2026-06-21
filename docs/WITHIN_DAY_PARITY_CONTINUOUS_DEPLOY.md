# Within-Day Parity — the CONTINUOUS-DEPLOYMENT fix-loop (Phase 3 design, DESIGN ONLY)

Status: **DESIGN SPEC — not activated.** Authored 2026-06-20 per Ben's sharpened Phase-3 model (relayed via
the coordinator). The cert-ledger SQL apply + the `dry_run=False` flip are held by the Lead for the
sequenced activation; this document designs the *process* + names the **first buildable step**. It builds
directly on the merged WDPC Phase 1 (the settled-window compare + settle-lag, `within_day_parity.py`) and
Phase 2 (the root-cause classifier `within_day_rootcause.py` + the cert/trust-grant writer
`within_day_trust.py`), and grounds every mechanism in what already exists (the feature-vector BUS + the fc
bind-mount/relaunch deploy).

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
  6. DEPLOY (§3): the in-scope fix reaches live fc → fc re-emits the fixed compute.
  7. CONFIRM IN PRODUCTION: the bus tripwire fires when live values change post-deploy → re-run step 1 on
     the now-fixed live stream → confirm PRODUCTION real-time == backfill → then CERTIFY.
```

Certification reuses Phase 2 exactly: `within_day_trust.write_certifications` (cert stamp + grant trust with
`reason='within_day_parity'`, one clean day → TRUSTED), held at `dry_run` until the Lead flips it.

**Why the in-sandbox re-confirm (step 5) is the speed unlock:** the subagent does NOT need to wait for a
production relaunch to know its fix *works* — it proves `compute_latest == compute` on recent real data in
its own sandbox, deterministically, in seconds (this is literally the Phase-1 mechanism run on a recent
window with the candidate code). Production deploy (§3) is then only the *confirmation that the same fix
behaves identically live* — expected to pass by construction.

---

## 3. ⭐ INGREDIENT 3 — CONTINUOUS DEPLOYMENT INTO PRODUCTION (the hard decision)

**The constraint:** fc BIND-MOUNTS the live tree and runs it, but a code change is picked up ONLY at
RELAUNCH (`ops/nightly_relaunch.sh`, which re-seeds + recreates the container with warm-start). There is **no
hot-reload**. The golden rule forbids `docker restart` (relaunch only via the script). So "deploy fast" is in
tension with "warm-capture / relaunch-only."

### The three options

| Option | Mechanism | Production-confirm latency | Risk | Verdict |
|---|---|---|---|---|
| **(i) Sandbox-iterate-then-promote** | The subagent iterates ENTIRELY in its own sandbox on recent live data (no production touch), proves the fix in-sandbox (§2 step 5), AUTO-MERGES if in-scope (§4); production picks it up on the **next scheduled relaunch** (nightly, or a Lead-triggered one). | **Next relaunch** (hours, or sooner if the Lead triggers one) for the *production* cert; but the fix is *proven correct* in **seconds** in-sandbox. | LOWEST — zero production touch during iteration; one controlled relaunch applies it warm. | ⭐ **RECOMMENDED** |
| **(ii) Fast targeted / canary fc redeploy** | After auto-merge, immediately relaunch fc (via `nightly_relaunch.sh`, the only sanctioned path) — possibly a canary fc on a symbol subset first. | **Minutes** (one relaunch). | MEDIUM — each relaunch costs a warm-up + re-seed + a (brief) capture seam; doing it per-fix churns live capture. Acceptable only off-RTH or rarely. | Fallback, Lead-gated |
| **(iii) In-process hot-reload of the changed group** | Reload the group module in the live fc process without relaunch. | **Seconds**. | HIGHEST — no hot-reload exists; building one means mutating live capture state mid-session (ring buffers, session caches, the publisher schema) — exactly the warm-capture corruption the relaunch rule exists to prevent. | **REJECTED** (don't build) |

### Recommendation: (i) Sandbox-iterate-then-promote — with the cert in two stages

The subagent's value is *proving the fix correct fast*, which option (i) gives in **seconds** (the in-sandbox
`compute_latest == compute` re-confirm on recent live data). It then auto-merges in-scope (§4) and **stays
online**; production picks the fix up on the **next relaunch** (the nightly one, or a Lead-triggered
off-RTH relaunch when a batch of fixes has accrued — the same controlled deploy we already do, never a
per-fix churn). This honors warm-capture + relaunch-only *exactly* (no new deploy mechanic, no golden-rule
exception) and adds ZERO production risk during iteration.

**Two-stage certification** (resolves "certify when?"):
- **Stage A — SANDBOX-CERTIFIED** (seconds): the fix proven in-sandbox (`compute_latest == compute` on recent
  live data, in-scope, byte-eq elsewhere). Written as a cert stamp `status='fix_pending'` → the fix is
  correct + auto-merged, awaiting production.
- **Stage B — PRODUCTION-CERTIFIED** (next relaunch): after the relaunch, the bus tripwire confirms the live
  stream now matches backfill → cert flips to `status='certified'` + the trust grant fires. This is the
  honest "production real-time now matches" Ben asked for — it just arrives at next-relaunch, not seconds,
  and that's the SAFE latency (the alternative is churning live capture per fix).

So: **fix proven in seconds; production-confirmed at the next controlled relaunch.** The Lead can shorten the
production-confirm latency at will by triggering an off-RTH relaunch once a batch of in-scope fixes has
auto-merged — but never mid-RTH, never per-fix.

### Why this needs NO fingerprint/coordinated deploy (the crucial enabler)

A within-day parity FIX corrects the *live compute* so it matches the *backfill compute* for an UNTRUSTED
feature — i.e. it changes the fast-path code to agree with the canonical slow path, **WITHOUT changing the
feature's `group:name:version`**. The bus fingerprint is a hash over `group:name:version` lines (per the
schema registry), so **a same-version compute fix is FINGERPRINT-NEUTRAL** → no codec mismatch, no strategy
rebuild, no coordinated deploy. fc just re-emits the corrected values under the same fingerprint on the next
relaunch. (If a fix genuinely requires a version bump — a semantic change, not a parity correction — it
leaves the auto-merge lane and becomes a Lead-coordinated deploy; §4.)

---

## 4. INGREDIENT 4 — THE AUTO-MERGE-IF-IN-SCOPE GATE (the precise safe rule)

A subagent's fix PR AUTO-MERGES iff **ALL** of these hold (checked mechanically, fail-closed):

1. **OWNED-SCOPE ONLY** — the diff touches ONLY the assigned group's feature code (its `groups/<group>.py`
   + any group-private helper it owns). A diff outside that path → human review. (Mechanical: `git diff
   --name-only` ⊆ the group's declared file set.)
2. **FINGERPRINT UNCHANGED** — `BusSchema.from_registry()` fingerprint is byte-identical before/after the
   fix (no version bump, no add/remove/rename/reorder). (Mechanical: compare the two fingerprints.)
3. **PARITY GATE FLIPS** — on the failing window/sample, the fix turns the feature's verdict from MISMATCH
   to CLEAN (value_rate ≥ the feature's `min_pass_rate`), proven in-sandbox (`compute_latest == compute`).
4. **BYTE-EQ ELSEWHERE** — every OTHER group's output is byte-identical before/after the fix on the same
   sample (the fix is surgical; it perturbs nothing else). (Mechanical: re-materialize the sample for all
   groups pre/post, assert equality outside the owned group.)
5. **VALUE-CHANGE ONLY ON THE UNTRUSTED FEATURE** — the only cells that change are the assigned UNTRUSTED
   feature's (which is **not traded**, so a value change is safe by construction — §6). A trusted feature's
   values must NOT move (if they do, it's out of scope → human review).
6. **TEST + QA GREEN** — the group's unit tests + the parity suite pass; ruff/black/isort/mypy clean.

If ANY condition fails → **NOT auto-merged**; routed to the Lead for review (the existing worktree→PR path
the WDPC already uses). This is the same conservative default as everywhere else: auto-merge is the *fast
lane for the provably-safe surgical fix*; everything ambiguous goes to a human.

The check is a CI/pre-merge gate (a script the subagent runs + the merge requires green): it composes the
**existing** `ops/bus_compat_gate.py` (fingerprint/contract safety) with the WDPC in-sandbox parity proof +
the byte-eq-elsewhere check. Nothing new conceptually — it's the deploy-safety we already enforce, narrowed
to "owned untrusted group, fp-neutral, parity-flipping."

---

## 5. ORDERING — which untrusted group the Lead assigns next

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

## 6. WHY UNTRUSTED-SCOPE MAKES THIS SAFE (the foundation of the whole design)

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

## 7. FIRST BUILDABLE STEP (gate-read before building)

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
in-sandbox fix-proof) with ZERO production-deploy risk and ZERO auto-merge — exactly the Phase-by-phase
discipline that got Phases 1-2 in cleanly. The auto-merge gate (§4) + the deploy wiring (§3) + the live
cert/trust flip are the NEXT steps, each gate-read, each Lead-sequenced.

---

## 8. Open questions for the gate-read

1. **Assignment + ownership mechanics** — does the Lead assign via a DB lock row (recommended: a
   `within_day_assignment` table keyed by group + agent + day) or via the existing agent-spawn? The design
   assumes a DB lock so it's stateless across wakes and visible to the ordering query.
2. **Auto-merge authority** — auto-merge means a bot merging to `main` with no human. Even fully in-scope,
   does Ben want a SILENT auto-merge, or auto-merge-with-notify (merge + post to the Lead, who can revert)?
   Recommend auto-merge-with-notify for the first N fixes, then silent once trusted.
3. **Production-confirm latency policy** — is "next nightly relaunch" acceptable as the default
   production-cert latency, with the Lead triggering an off-RTH relaunch when a batch accrues? Or does Ben
   want a tighter SLA (which forces option (ii)'s per-fix relaunch cost)?
4. **The bus `FP_BUS=1` enablement** — the bus emit is gated OFF in prod today. Enabling it for the watch is
   itself a (tiny, off-hot-path, proven-safe) change to live fc — it should ride the same RTH-dent /
   Lead-sequenced discipline as the agent activation.
5. **Stability N + the two-stage cert semantics** — confirm N (≈2-3 clean windows) for SANDBOX-CERTIFIED vs
   the production re-confirm count for PRODUCTION-CERTIFIED.

---

## 9. Summary — the design in one paragraph

An untrusted, unworked, not-certified-today group is assigned (DB lock) to a bounded subagent. It SUBSCRIBES
to that group's live values via the bus (`poll_views`, the freshness tripwire) and authoritatively COMPARES
live-stream vs backfill on the settled window (Phase-1, same tolerance as nightly). On a real mismatch it
ROOT-CAUSES (Phase-2 classifier), FIXES in its own worktree, and PROVES the fix in **seconds** in-sandbox
(`compute_latest == compute` on recent live data, in-scope, fingerprint-neutral, byte-eq elsewhere). If the
fix passes the AUTO-MERGE-IF-IN-SCOPE gate (owned-group-only, fp-unchanged, parity-flips-clean,
byte-eq-elsewhere, value-change-only-on-the-untrusted-feature, tests green) it auto-merges; otherwise a human
reviews. Production picks the fingerprint-neutral fix up on the **next controlled relaunch** (warm-capture +
relaunch-only honored exactly — NO hot-reload, NO golden-rule exception), and the bus tripwire confirms the
live stream now matches → the cert flips to PRODUCTION-CERTIFIED + trust is granted. Safe because an
untrusted feature is never traded, so an in-flight value change can't affect a position; the only risks
(perturbing a trusted feature or the fingerprint) are exactly what the auto-merge gate blocks. **Recommended
production-deploy: sandbox-iterate-then-promote (option i)** — fix proven in seconds, production-confirmed at
the next relaunch, zero production touch during iteration.
