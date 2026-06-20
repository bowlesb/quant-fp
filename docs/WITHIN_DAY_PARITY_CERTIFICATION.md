# Within-Day Parity Certification — process design (DESIGN ONLY, gate-read before build)

Status: **DESIGN SPEC — not built.** Authored 2026-06-20 per Ben's direction (relayed via the
coordinator). Gate-read with the coordinator before any implementation. This document specifies the
*process*; it proposes one net-new DB stamp + one new agent loop and otherwise **reuses** the existing
parity/trust/defect infrastructure (`validation_sweep.py`, `compare.py`, `cleanliness.py`,
`trust_lifecycle.py`, `trust_binary.py`, `12_trust_binary.sql`, the `feature_parity_defect` auto-close
#161). It is the **T+0 (intraday) sibling** of the always-on T+1 nightly sweep in `PARITY_PLAYBOOK.md`.

---

## 0. What Ben asked for (the loop, verbatim intent)

> A dedicated agent would, in real-time, monitor the values a feature group emits, measure that against
> what the backfill API + backfill code path produce, compare them directly, and if they don't match,
> update code until they match. Watch for a while, ensure they keep matching for some period, then
> "leave it" — mark it "reviewed within-day parity on X date" (nothing left to do on that feature that
> day), move to the next feature.

So the deliverable is a **per-feature, market-hours, live==backfill validate → fix → certify loop**, one
feature (group) at a time, that ends each feature in a durable "reviewed today" stamp and advances to the
next. The agent is the **Within-Day Parity Certifier (WDPC)**.

### Why this is different from the nightly sweep (the reason it's net-new)

The T+1 nightly sweep (`validation_sweep.sweep_day`) grades the **previous, fully-settled** day in one
batch and feeds the binary-trust lifecycle. The WDPC runs **during market hours on the current day**,
on a **rolling recently-settled window**, one feature group at a time, with a **tight investigate→fix→
re-check inner loop** — so a freshly-refactored feature (latency Rust/incremental rewrite, see §6) can be
re-validated and re-certified *the same day* rather than waiting for the nightly batch. It is a focused,
interactive, code-fixing instrument; the sweep is a broad, hands-off grader. They share every comparison
primitive and write to the same ledgers.

---

## 1. THE LOOP (per feature group)

The unit of work is a **feature group** (the registry's compute/version unit — features in a group share
inputs + compute + version, and parity is earned per group), iterated feature-by-feature within the group
for reporting. One WDPC cycle = one group taken from PENDING → certified-or-defected.

```
for group in ordering (§4):
  loop every CYCLE_INTERVAL (≈ 60s) on the most-recent SETTLED minute window (§2.1):
    (a) SAMPLE LIVE   : read source=stream cells the group emitted for (symbol, minute) in the window
    (b) COMPUTE BACKFILL: materialize the SAME (group, day, symbols, window) from /store/raw via the
                          backfill code path → source=backfill cells
    (c) COMPARE       : compare.cell_verdict() per feature, per the group's parity_method + tolerance
    (d) if MISMATCH (on CLEAN symbols only, §2.3): ROOT-CAUSE + FIX (§1.1) → worktree→PR → (after merge
                          + fc relaunch by the Lead) resume the loop on the new code
    (e) if MATCH     : advance the STABILITY counter (§3). Reset it on any clean-symbol mismatch.
    until STABILITY reached (N consecutive clean cycles) OR a fix is pending (hand off, see §1.2)
  (f) CERTIFY: stamp "within-day parity reviewed on <date>" for the group (§3 ledger), with evidence,
              and advance to the next group.
```

### 1.1 Root-cause map (where a mismatch lives)

The compare diff already localizes the failing cells (`feature_validation_exception` exemplars:
symbol, ts, stream_value, backfill_value, rel_err). The WDPC maps the signature to the code path:

| Mismatch signature | Likely culprit (live side) | Likely culprit (backfill side) |
|---|---|---|
| Windowed reduction off by float-eps on near-flat windows | `incremental.py` / `aggregates.py` (Welford/centered-std cancellation) | `raw_loaders.py` rolling-sum order |
| Off only on illiquid / thin names | warm-up or coverage (NOT a bug — §2.4) | thin-session denominator |
| Off only on tick/quote features for non-subscribed symbols | FP_TICK_SYMBOLS coverage gap (NOT a bug — §2.4) | — |
| Sign / direction flip | `stateful.py` signed-flow accumulation | `raw_loaders.load_raw_trades` signing |
| Stream NaN vs backfill NULL on degenerate window | unguarded `value>threshold` after a kernel NaN (the bb/#122 + OLS/#131 class) | — |
| Whole-group constant divergence | gather fragmentation (NOT a bug — §2.4) or a real group-version skew | — |

The fix is a **code change via worktree → PR** (never the live tree; the WDPC follows the same boundary
as every agent). Feature-definition changes that alter output **bump the group version** and are
**fingerprint-affecting** → they go through the **Lead-coordinated deploy** (§5), never unilaterally.

### 1.2 The async reality: WDPC proposes fixes, the Lead deploys

A code fix can only take effect live after a `make dev-image` rebuild + an fc relaunch (golden rule:
never `docker restart` fc; relaunch only via `ops/nightly_relaunch.sh`). The WDPC therefore **cannot
close a fix-requiring feature in a single cycle.** The loop state for such a feature is **`FIX_PENDING`**:
the WDPC ships the PR, records the defect, hands off to the Lead, and **moves to the next feature**;
on a later cycle (after the Lead merges + relaunches at a market-closed window, or for a non-fingerprint
store/loader fix, after the next materialize) it resumes and re-checks. Certification is reserved for
features that reach stability **without a pending fix**.

---

## 2. THE HARD PARTS (and how the design solves each)

### 2.1 SETTLE-LAG — never compare the live tail

During market hours the current minute is **provisional** (the SIP hasn't applied
corrections/cancels; backfill API hasn't served it). Comparing the live edge would manufacture false
mismatches. The WDPC compares only on a **recently-settled rolling window**:

- **Settled-window definition:** minutes in `[now − SETTLE_LAG − WINDOW_LEN, now − SETTLE_LAG]`, where
  `SETTLE_LAG` is sized from the **measured** intraday backfill availability (the `news_lag`-style
  empirical p90 of "minute close → that minute's settled bars/ticks queryable via the backfill API").
  Start conservative (`SETTLE_LAG ≈ 15–20 min`, the historical-bars intraday delay) and **measure it**
  before trusting tighter — a `settle_lag.py` probe analogous to the existing `news_lag.py`.
- **Per-layer lag:** bars settle fastest, trades slower, sub-minute/quotes slowest
  (`PARITY_PLAYBOOK.md §2` layers A/B/C). `SETTLE_LAG` is **per-layer** (read from the group's
  FeatureType), not one global constant.
- **Reuse:** this mirrors `assert_raw_present` / `assert_tail_settled` from the sweep, applied to a
  **window inside the live day** instead of the whole prior day. The same `RawNotSettledError` clean-skip
  semantics apply — if the window's raw hasn't landed, the cycle SKIPS (no grade), it does not fail.

### 2.2 FLOAT TOLERANCE — reuse, don't reinvent

Use the **exact existing tolerance machinery**: `compare.match_predicate()` =
`|live − back| ≤ 1e-12 + rtol·|back|` with the per-FeatureType / per-feature `rtol` + `min_pass_rate`
from `trust_policy.py` (CALENDAR exact; PRICE/VOLUME 1e-6 @ 0.9999; TECHNICAL/VOLATILITY/tick 1e-4 @
0.999; distributional method for Layer-C tick-order-sensitive features). The WDPC introduces **no new
tolerance** — a within-day "match" means exactly what a nightly-sweep "match" means, so a within-day
certification is directly comparable to (and feeds) the same trust grade.

### 2.3 CONTAMINATION — only grade CLEAN symbols, only on COHERENT minutes

A within-day window can be contaminated exactly like a full day. Reuse `cleanliness.py` verbatim,
windowed:

- Per-(symbol, window) cleanliness via the same three signals (`max_gap_minutes ≤ 5`,
  `coverage_frac ≥ 0.90`, `n_backfill_minutes ≥ floor`) — the floor scales to the window length, not 120
  (an **open question**, §7: what is the minimum settled-minute count for a fair *windowed* grade?).
- Cross-sectional groups gated by `gather_coherence()` — a fragmented-gather minute is not graded.
- A cycle contributes a grade only if `≥ MIN_CLEAN_SYMBOLS` clean symbols are in the window (reuse the
  20-symbol floor, possibly relaxed for the short window — §7).

### 2.4 THE KNOWN NON-BUGS — must not be "fixed"

The WDPC must recognize and **NOT chase** the three settled artifacts (the memory of prior cycles is
explicit here):

1. **FP_TICK_SYMBOLS coverage gap** — tick/quote features are null for non-subscribed symbols on BOTH
   sides → `runnable()` skips them → no divergence filed. A live≠backfill on a tick feature for a
   non-tick symbol is a **coverage artifact, not a math bug** (root-caused 2026-06-19). The WDPC scopes
   tick-group comparison to the `tick_symbols()` set only.
2. **Warm-up exclusion** — windows > the 90-min warm-up are warming but parity-consistent (both sides
   truncate to the capture anchor). The `rth_mask` + warm-up handling already excludes these. A
   within-day window early in the session may be inside warm-up for long windows → **not graded**.
3. **Capture-start artifact** — if fc started capture mid-session, long-window live cells cover a
   different bar SET than backfill (the 06-18 artifact). The cleanliness coverage floor catches this; the
   WDPC treats a coverage-driven divergence as contamination, **not a code defect**.

These are encoded as **pre-checks** before any "update code" action: a mismatch is only a fix candidate
if it survives (clean symbols) ∧ (coherent minutes) ∧ (in-coverage windows) ∧ (tick-scope for tick groups).

### 2.5 STABILITY / "leave it" criterion — see §3.

### 2.6 FEATURE ORDERING — see §4.

---

## 3. THE STABILITY CRITERION + THE CERTIFICATION LEDGER

### 3.1 "Watch for a while" — the leave-it rule

A group is **certifiable for the day** when, **without a pending fix**, it accumulates
`MIN_STABLE_CYCLES` **consecutive** clean cycles (proposed default: **N = 3** cycles over **≥ 30
contiguous settled minutes**, no clean-symbol mismatch in any of them), at a value_rate ≥ the group's
`min_pass_rate` on each. Any clean-symbol mismatch **resets the counter to 0**. This is the within-day
analogue of the nightly "clean day"; the exact N and minute-span are tunable (§7) — the principle is
*sustained* match, not a single lucky minute.

### 3.2 The certification stamp (NET-NEW — confirmed nothing equivalent exists)

A new append-only table records the per-feature, per-day review:

```sql
-- db/init/13_within_day_parity.sql  (proposed)
CREATE TABLE IF NOT EXISTS within_day_parity_cert (
  feature            text        NOT NULL,
  version            text        NOT NULL,        -- (feature, version) like the rest of the trust model
  group_name         text        NOT NULL,
  cert_day           date        NOT NULL,        -- the day the within-day review happened
  certified_at       timestamptz NOT NULL DEFAULT now(),
  status             text        NOT NULL,        -- 'certified' | 'fix_pending' | 'skipped_unsettled'
                                                  --            | 'skipped_contaminated' | 'defected'
  stable_cycles      int         NOT NULL,        -- consecutive clean cycles achieved
  window_minutes     int         NOT NULL,        -- contiguous settled minutes the match held over
  value_rate         double precision,            -- worst per-cycle match rate across the stable run
  n_clean_symbols    int,                         -- breadth of the evidence
  tolerance          double precision,            -- the rtol the match was judged at (provenance)
  min_pass_rate      double precision,
  git_commit         text,                        -- code the live side ran (provenance / replay)
  content_hash       text,                        -- the group's content hash at cert time
  reason             text,                        -- human note (esp. for skipped/fix_pending/defected)
  PRIMARY KEY (feature, version, cert_day)        -- one stamp per (feature,version) per day; idempotent UPSERT
);
```

**What `status='certified'` ASSERTS (precisely):** *"On `cert_day`, version `version` of this feature was
observed emitting LIVE values that matched its BACKFILL recomputation, on the recently-settled intraday
window, within tolerance `tolerance` on ≥ `min_pass_rate` of `n_clean_symbols` clean symbols' cells, held
stable for `stable_cycles` consecutive cycles over `window_minutes` contiguous settled minutes — and there
is nothing left to do on this feature today."* It is a **daily operational checkpoint** ("reviewed,
nothing outstanding"), distinct from the permanent binary `feature_trust.trust_state` grant.

The other `status` values carry the rest of Ben's loop honestly: `fix_pending` (a PR is out, recheck
after deploy), `skipped_unsettled` / `skipped_contaminated` (no fair test today — try tomorrow),
`defected` (a real divergence the WDPC filed but couldn't fix this cycle).

### 3.3 How it feeds the trust lifecycle (REUSE the binary model)

The certification is **evidence**, not a parallel trust state. A `status='certified'` row on a clean day,
at the group's tolerance + min_pass_rate, **is exactly the condition** `trust_binary.earned_features()`
uses to grant `feature_trust.trust_state='TRUSTED'` (one clean day, per `TRUST_REDESIGN.md`). So:

- The WDPC writes the cert stamp **and** calls the existing `trust_binary` grant path with
  `trust_reason='within_day_parity'` (a new reason value alongside `deterministic`/`parity_1day`) — a
  within-day-certified feature **earns binary trust the same way** a nightly-clean feature does, just
  earlier in the day. No new trust state.
- A `defected` cert opens / refreshes a `feature_parity_defect` row via the **existing**
  `trust_lifecycle.defect_rows()` path; the **existing auto-close #161** closes it after
  `AUTO_CLOSE_STREAK` clean recurrence-free sweeps (the WDPC's clean recurrences count toward the same
  streak). The WDPC reuses the defect ledger wholesale; it does not invent a second defect store.

---

## 4. FEATURE ORDERING — which feature next

The WDPC pulls the next group from a **priority queue**, highest-value first:

1. **JUST-REFACTORED first** (§6) — any group whose version was just bumped by a latency refactor and is
   now NON_TRUSTED for the new version is top priority: it lost trust by construction and the WDPC is the
   mechanism that re-earns it. Source: `feature_trust` rows where `version` = the live registry version
   and `trust_state='NON_TRUSTED'` and the group's content_hash changed recently.
2. **UNTRUSTED-but-trustable** — groups NON_TRUSTED for the live version that are not deterministic and
   have a backfill side to compare (the charter's "stuck" set).
3. **TRUSTED-but-unreviewed-today** — a light re-affirmation pass over already-trusted groups (cheap
   insurance; the within-day analogue of the random re-check), lowest priority.
4. **Deterministic groups** (CALENDAR / static maps) are auto-certified without a live window (parity is
   structural) — they short-circuit to `status='certified', reason='deterministic'`.

Within a tier, order by **trade-relevance** (groups feeding live strategies first) then by **last cert
age** (longest-unreviewed first). The queue is re-derived each cycle from the DB so it self-heals across
agent wakes (no in-memory state to lose).

---

## 5. INTEGRATION with existing infra (REUSE matrix)

| Need | REUSE (existing) | NET-NEW |
|---|---|---|
| Live value sample | `store.get_features(source='stream')` (what `compute_latest` wrote) | windowing to the settled sub-day window |
| Backfill value | `materialize.materialize_from_raw[_full]` + `raw_loaders` | per-window (not per-day) materialize scope |
| Compare / tolerance | `compare.cell_verdict` + `match_predicate` + `trust_policy` rtol | — |
| Contamination gate | `cleanliness.symbol_day_cleanliness` + `gather_coherence` | windowed-cleanliness floor (§7 open) |
| Settle gate | `assert_raw_present` / `assert_tail_settled` / `RawNotSettledError` | intraday `settle_lag.py` probe (per-layer) |
| Binary trust grant | `trust_binary.earned_features` / `write_trust_grants` | `trust_reason='within_day_parity'` value |
| Defect ledger + auto-close | `feature_parity_defect` + `trust_lifecycle.auto_close_updates` (#161) | — |
| Per-day cert stamp | — | `within_day_parity_cert` table (§3.2) |
| The agent loop | the WDPC agent (this spec) + the existing worktree→PR + Lead-deploy boundary | the WDPC driver itself |
| Refactor → re-validate | `TRUST_REDESIGN.md` version-bump model (new version starts NON_TRUSTED) | the §4 "just-refactored first" queue priority |

**Boundaries (same as every agent):** worktree→PR off origin/main, never the live tree; fingerprint /
version-bumping changes go through the Lead at a market-closed window; never restart fc; memory-bounded;
never print secrets. The WDPC writes only the cert stamp + (via existing paths) trust/defect rows — it
**proposes** code fixes, it does not deploy them.

---

## 6. ⭐ THE TIE-IN: re-validating LATENCY-REFACTORED features

Ben authorized refactoring features for speed (Rust kernels / incremental engine) **even if they lose
trust and must re-validate**. The within-day-parity loop **is** that re-validation mechanism. The
mechanics fall straight out of the existing model:

1. A latency refactor changes a group's compute → **bumps the group version** (`TRUST_REDESIGN.md`
   decision 2). The new `(feature, vN+1)` row starts `NON_TRUSTED` — trust is not "demoted", the new code
   simply hasn't earned it yet. (If the refactor is provably value-identical — byte-equal parity — it can
   ship under the *same* version and keep trust; the WDPC is the instrument that *proves* byte-equality
   before that call. The default, conservative path is version-bump + re-earn.)
2. After the Lead deploys the refactored fc, the WDPC's §4 queue puts the just-bumped group **first**.
3. The loop runs: live (new fast path) vs backfill (the canonical slow path = the source of truth) on the
   settled window. If they match to tolerance and hold stable → certify + grant trust under the new
   version, **same day**. If they diverge → the refactor introduced skew; the WDPC root-causes
   (incremental/Rust kernel vs the batch canonical), files a defect, and the refactor PR is **not** safe
   to keep — it must be fixed until parity holds. This is the safety net that lets latency work move fast:
   *speed changes are allowed to break trust because there is a fast, same-day mechanism to re-earn it.*
4. The certification record (`git_commit`, `content_hash`, value_rate) is the **audit trail** that the
   fast version reproduces the canonical one — exactly what the Verification-culture bar requires before a
   refactored feature is trusted at live scale.

---

## 7. OPEN QUESTIONS (for the gate-read)

1. **SETTLE_LAG calibration.** Build `settle_lag.py` (empirical p90 of minute-close → settled-bars-
   queryable) before fixing the constant? Per-layer values? — recommend YES, measure first (mirrors the
   `news_lag` discipline; un-measured lag = false mismatches).
2. **Windowed cleanliness floor.** The nightly floor is `n_backfill_minutes ≥ 120` (1/3 session). A
   30-minute settled window can't meet that. What is the minimum settled-minute count + min-clean-symbols
   for a *fair short-window* grade? Proposed: scale the floor to the window (e.g. ≥ 0.8·window_len
   minutes) and keep `MIN_CLEAN_SYMBOLS=20`, but this needs a measured false-positive check.
3. **Stability N + span.** Is N=3 cycles / ≥30 min the right "for a while"? Trade-off: longer = stronger
   evidence but fewer features certified per day. Recommend starting N=3/30min and tuning from observed
   flake rate.
4. **Materialize cost intraday.** A per-window `materialize_from_raw` over the clean-symbol set is cheaper
   than a full-day materialize, but still real compute. Does the WDPC run on `feature-computer` (shares
   the live capture box) or a separate sandbox? Memory-bound either way; must **never** starve live
   capture (the `live_monitor` guard). Recommend a dedicated guard-named container, one group at a time.
5. **Deploy cadence vs market hours.** A fix that bumps the fingerprint can only deploy at a market-closed
   window — so within-day *certification* of a fix-requiring feature is inherently next-session. Is that
   acceptable (cert = "reviewed, fix queued") or does Ben want same-session deploy for non-fingerprint
   (store/loader) fixes only? Clarify the "leave it today" semantics for `fix_pending`.
6. **Relationship to the nightly sweep.** Does a within-day `certified` pre-empt that night's sweep grade
   for the same feature (avoid double-grading), or do both run and must agree (a stronger cross-check)?
   Recommend: both run, and a within-day `certified` that the nightly sweep then contradicts is itself a
   high-signal defect (intraday-settled ≠ fully-settled → a settle-lag or correction-sensitivity bug).
7. **Agent cadence / durability.** The WDPC is a market-hours loop; the agent fabric is currently
   session-only (the known durability gap). Does this run as a Lead-sequenced loop during RTH, and how
   does it resume mid-session across wakes (answer: all state is in the DB queue + cert ledger, so it is
   stateless across wakes by construction — but the *cadence* needs a scheduler).

---

## 8. Reusable vs net-new — the honest summary

**Reusable wholesale (no change):** `compare.py` (cell_verdict/match_predicate/dist_score), `trust_policy.py`
(tolerances), `cleanliness.py` (contamination signals + gather_coherence), `materialize.py` + `raw_loaders.py`
(the backfill code path), `store.get_features` (live + backfill read), `trust_binary.py` (the grant),
`feature_parity_defect` + auto-close #161, the `RawNotSettledError` clean-skip, the worktree→PR + Lead-deploy
boundary, the `TRUST_REDESIGN.md` version-bump model.

**Net-new (small):** (1) the `within_day_parity_cert` table + a `trust_reason='within_day_parity'` value;
(2) an intraday `settle_lag.py` probe (per-layer); (3) a **windowed** wrapper over the existing
cleanliness/settle gates (sub-day window instead of whole prior day); (4) the WDPC agent driver itself (the
ordering queue §4 + the cycle loop §1, all DB-backed/stateless-across-wakes); (5) one open tuning of the
windowed-cleanliness floor + stability N.

**The big reuse insight:** the within-day process is **the nightly sweep run on a rolling settled window
with a tighter human-in-the-loop fix step and a per-day cert stamp.** Almost everything already exists; the
genuinely new thing is the *cert stamp* + the *intraday settle-lag* + the *agent that drives the focused
fix loop and orders features by re-validation priority.* That keeps the build small and the trust model
single-sourced.
