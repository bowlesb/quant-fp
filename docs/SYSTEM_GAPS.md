# SYSTEM GAPS — adversarial gap-hunt (deploy · pipeline · strategies · execution · observability)

Produced 2026-06-21 in response to Ben's complaint that "huge gaps are too easy to find" → a proactivity
failure. This is an **evidence-based** sweep: every gap below was confirmed against live container state,
config, or code on 2026-06-21 ~19:10Z — not assumed from ledgers. It is read-only; closing the gaps is NOT
done in this pass. Companion to `READINESS.md` (which tracks built-but-not-proven *capabilities*); this doc
tracks the *operational holes* between "built" and "actually armed/wired/monitored."

Ranked by impact. Each gap: WHAT (+evidence) · WHY it matters · EXACT next action · suggested OWNER.

---

## TOP GAP (the single most important)

### G1 — Continuous deployment is NOT armed: no daemon runs, PRs sit ungraded, subagents cannot self-deploy
**Stage in READINESS:** "CI BUILT, PROVEN, NOT WIRED-AS-DAEMON" — but the reality is stronger than that row implies.

**Evidence:**
- `ps aux | grep ci_watcher|ci_deploy` → **nothing running.**
- `~/.quant-ops/ci_watcher.log` and `~/.quant-ops/ci_deploy.log` → **do not exist** (the daemons have never run).
- No systemd unit and no crontab entry references `ci_watcher` / `ci_deploy` (crontab only has healthcheck,
  feature_scan, live_monitor, data_freshness, daily/nightly lifecycle).
- PR **#361** (quote-breadth-backfill) is OPEN right now with **no CI grade** posted — it just sits there.
- The two main autonomous loops (`autonomous_loop.sh`, `audit_loop.sh`) are **commented out / PAUSED** in cron.

**Why it matters:** The entire premise — "a subagent fixes its feature group and the fix reaches prod" — is
currently **fiction**. Nothing grades PRs, nothing auto-merges TIER-1, nothing auto-deploys. Every PR waits
for the Lead to manually run `--once`. This is the keystone Ben funded (`TaskCreate #69`), and it is built but
**dark**. Memory `project-realtime-cd-feature-fix-flow` records this as a hard Ben requirement.

**Exact next action (the arming sequence):**
1. Install `ops/ci_watcher.sh ci` as a **supervised daemon** (systemd-user unit or a `@reboot` + cron-respawn
   guard) — Phase 1 (grade-only) first. Confirm `ci_watcher.log` fills and PR #361 gets a grade.
2. Soak Phase-1 for N PRs; confirm TIER classification + fail-closed scope behave. Then remove `--no-auto-merge`
   → Phase-2 (auto-merge TIER-1).
3. Install `ops/ci_watcher.sh deploy` (`ci_deploy.py`) as a second daemon → Phase-3, first real auto-deploy =
   a dashboard-only (fp-neutral) change, observed.
4. Add a watchdog: if either daemon's log is stale > X min during business hours, the healthcheck FAILs loudly.

**Owner:** Lead-gated arming click → then ContinuousDeploy workstream owns the daemon supervision + watchdog.

---

## DEPLOY / CD

### G2 — WDPC subagent→live-fc hot-swap is NOT wired to the running fc (offline-tested only)
**Evidence:** `quantlib/features/within_day_applier.py` + `hot_swap.py` exist, but inside the live
`feature-computer` container there is **no applier process** (`docker exec feature-computer ps aux` shows none)
and **no swap/applier log lines** in `docker logs feature-computer`. The fc reader/workers warm-start and run
the static registry; nothing watches a deploy queue or hot-swaps a group between minutes.
**Why:** The "real-time CD for feature fixes" (memory `project-realtime-cd-feature-fix-flow`) needs a SAFE
per-group hot-swap in the live fc. Today a subagent's merged fix only reaches the live fc on the **next
nightly relaunch** (`nightly_relaunch.sh`), not "immediately" as required.
**Exact next action:** wire a deploy-queue consumer into the fc process (or a sidecar that signals it) that
calls the `up_to_date()`/`rebuild_from_history` contract (#353) via the refactored applier (#357), gated to
fp-neutral/untrusted/single-group swaps; rehearse on crypto-capture first (24/7, low blast radius).
**Owner:** CryptoE2E (rehearsal) → Lead (live-fc wiring click).

### G3 — `latency recompute cron` not wired; latency metrics not emitted live
**Evidence:** healthcheck shows `WARN bar_to_vector_latency: latency metrics not emitted (deploy pending)` and
`WARN group_compute_p99: feature_group_compute_seconds not emitted`. READINESS row "latency recompute cron —
NOT WIRED (by design)."
**Why:** Ben's progress axis D (bar→vector latency) and axis E (transparency) are unobservable live right now.
**Exact next action:** move the latency JSON out of the FF-deploy (bind-mounted) tree, then wire the 02:xx cron.
**Owner:** Lead.

---

## STRATEGIES (live health)

### G4 — Strategies are barely trading and are silently stuck; stale/abandoned orders pollute the live paper account
**Evidence (Alpaca paper account `equity=$100,016`, queried live):**
- **5 OPEN market orders stuck in `accepted` (never filled):**
  - 3 × smoke exits (`smoke_{SPY,AAPL,AMD}_20260618...exit`) created **2026-06-18**, still "accepted" 3 days later.
  - 2 × `ittest...KO buy` orders created **2026-06-20** — **test pollution** (see G8).
- **smoke-strategy** is stuck in an infinite loop: `docker logs smoke-strategy` repeats
  `CLOSE pending fill for {SPY,AAPL,AMD}` every ~2s — it can't close positions whose exit orders are
  perpetually "accepted." 3 stranded fractional positions (AAPL/AMD/SPY) result.
- **reversion-strategy**: last activity **2026-06-20 08:08**, where it logged `ABANDON: entry ... never
  landed; marking bet terminal` for AMZN+AAPL (entry orders never filled). Idle since.
- **overnight-beta-strategy**: log shows only two `starting:` lines (06-20 08:09 and 16:49) — **no evidence of
  actual rebalancing/trading**, just restarts.
**Why:** Strategies that look "Up" in `docker ps` are not healthy. Market orders sit "accepted" because they're
submitted while the market is closed and **nothing cancels stale day-orders** → the close loop never completes,
positions strand, and the paper PnL is meaningless. This is exactly the "looks done, isn't" class Ben hates.
**Exact next action:**
1. Add **stale-order reconciliation**: on startup and each loop, cancel `accepted`/`new` orders older than a
   threshold (or use DAY tif + cancel-on-close) before resubmitting. The execution layer (#214) has
   reconcile primitives — wire them into the strategy loops so a never-filled order is cancelled, not retried forever.
2. Cancel the 5 stuck orders + flatten the 3 stranded smoke positions to reset the paper account to clean.
3. Verify overnight-beta actually places its L/S basket (instrument the rebalance path or assert orders posted).
**Owner:** a StrategyHealth workstream (new) — strategies cannot be trusted as a PnL signal until this is fixed.

### G5 — overnight-beta-strategy pegged at 173% CPU and 96% memory (OOM-risk), no trading to show for it
**Evidence:** `docker stats` → `overnight-beta-strategy 173.05% CPU, 962.3MiB / 1GiB`. The Lead flagged a
"persistent ~3-core" concern — confirmed, and worse: it's at **96% of its 1GiB limit** = imminent OOM-kill.
Meanwhile (G4) it shows no trading. So it is burning ~2 cores continuously for no observable output.
**Why:** Wastes the 32-core box, risks an OOM crash that `live_monitor` would silently restart (PROTECTED set),
masking the real problem. A hot-spinning strategy with no trades is a bug, not a workload.
**Exact next action:** profile the overnight-beta loop (likely a tight no-sleep poll or a heavy per-tick beta
recompute over 60-day windows with no caching); add a sleep/backoff + cache the beta matrix; raise/right-size
the mem limit only after the leak is understood.
**Owner:** StrategyHealth / whoever owns overnight_beta.

### G6 — crypto-momentum strategy (#345) is NOT launched — no container exists
**Evidence:** `docker ps -a` has **no** crypto-momentum container. `docker-compose.crypto-strategy.yml` exists
with `CMOM_ENABLED: "0"` (live dry-run default). The `fv:crypto:{BTC,ETH,SOL,DOGE,LTC}USD` bus streams ARE
populated by crypto-capture, so the input is ready — but nothing consumes them for trading.
**Why:** READINESS lists "crypto LIVE paper strategy — BUILDING"; the gap is that the code is merged (#345) but
the **dry-run container was never started**, so the bus→strategy→paper loop has never run live even in dry-run.
**Exact next action:** `docker compose -f docker-compose.crypto-strategy.yml up -d` with `CMOM_ENABLED=0`
(dry-run), observe the consume+log loop clean for a session, THEN Lead flips `CMOM_ENABLED=1`.
**Owner:** CryptoStrategy (launch dry-run) → Lead (flip to paper).

---

## EXECUTION + STATE (#214)

### G7 — Execution+state layer is wired into strategy code but the real-Alpaca paper proof is opt-in, not a standing gate
**Evidence:** `strategies/reversion/__main__.py` constructs the real `TradingClient(...paper=True)` and the
`PaperAlpacaExecutor` path is used by reversion/overnight/smoke/crypto. The real-broker conformance test
`test_live_paper_account_integration` exists but is **opt-in** (`@skipif RUN_ALPACA_PAPER_IT!=1`) — it is the
sim==paper proof, run manually. So #214 IS live-wired (contradicts READINESS "NO LIVE WIRING"), but its
correctness gate (sim==paper against the real broker) is **not run on a schedule** and there's no record it
passed cleanly recently.
**Why:** The stranded/stuck orders in G4 are direct evidence the live execution path has an unverified hole
(stale-order handling) that the opt-in test doesn't exercise as a standing gate.
**Exact next action:** (1) update READINESS — #214 is live-wired, the gap is the *standing proof*; (2) run
`RUN_ALPACA_PAPER_IT=1 pytest test_paper_alpaca_executor.py` as a recorded gate (Lead, with creds); (3) extend
the conformance to cover the stale-day-order case from G4.
**Owner:** Lead (run the gate) + StrategyHealth (extend coverage).

### G8 — The live paper account is polluted by integration-test orders (`ittest...`)
**Evidence:** 2 OPEN orders `ittest406234-...-KO-buy` and `ittestb3cdd4-...-KO-buy` (2026-06-20) in the REAL
paper account. The `ittest` prefix comes from the opt-in `test_paper_alpaca_executor.py` /
`test_within_day_phase2.py` integration tests running against the **shared live paper account**.
**Why:** Test orders mixed into the live account corrupt PnL/position accounting and leave stuck "accepted"
orders (G4). Tests must not leave residue in the trading account that strategies also use.
**Exact next action:** the IT must (a) use a clearly-namespaced coid, (b) **cancel/flatten its own orders in
teardown**, and ideally (c) run against a separate paper sub-account. Cancel the 2 stuck `ittest` orders now.
**Owner:** Lead / execution-layer owner.

---

## OBSERVABILITY / MONITORING

### G9 — Nothing ALERTS. The continuous healthcheck has been FAILING 5/15 checks all day and no one is paged
**Evidence:** `ops/healthcheck.sh` latest = `HEALTHCHECK 6 PASS / 4 WARN / 5 FAIL`. It only **appends to a
jsonl** (`~/.quant-healthcheck/healthcheck.jsonl`); there is **no notifier** — `grep -r slack|webhook|pager|
smtp ops/` finds only `data_freshness.sh` (and that's just a status field, not a pager). Prometheus has one
`ingestor_alerts.yml`; Grafana has **no alerting provisioning** (`/etc/grafana/provisioning/alerting/` absent).
**Why:** This is Ben's implicit point — "are we monitoring proactively?" The answer is **no**: we'd only learn
fc/strategy/capture broke by a human glancing at a jsonl or the dashboard. A real outage (like the 06-17 fc
37-min outage in memory) would not page anyone.
**Exact next action:** add ONE notifier (Slack webhook or email via the existing creds) fired by the healthcheck
cron when FAIL count > baseline, and by `live_monitor` when it restarts a PROTECTED container. Provision Grafana
alerts for fc-emission-stall + strategy-order-stall.
**Owner:** a new Observability workstream (or fold into Maintainer).

### G10 — The healthcheck is not market-day-aware: 5 checks FALSE-FAIL every weekend/holiday, drowning real signal
**Evidence:** all 5 FAILs today share root cause `expanded paths were empty ...
group=volume/...source=stream/date=2026-06-21/*.parquet` — i.e. **Sunday has no equity stream**, so the
store-coverage checks (`newest_minute_age`, `distinct_symbol_coverage`, `per_minute_active`,
`alphabetical_bias`, `ohlc_invariants`) hard-FAIL instead of SKIP. (Verified 06-21 = Sunday; `fv:SPY` bus last
entry = 2026-06-19 00:00, the prior Friday session — correct.)
**Why:** Compounds G9: even if we *did* alert, it would page every weekend for nothing → alert fatigue →
real failures ignored. A monitor that can't tell "market closed" from "capture broke" is not trustworthy.
**Exact next action:** gate these 5 checks on an RTH/market-calendar predicate (SKIP off-session, like
`data_freshness` already does for edgar/news with "off business-hours — lull expected, not graded").
**Owner:** Maintainer / Observability.

### G11 — `universe_membership=30656` vs expected ~11,336 (+170% drift) — unexplained, only a WARN
**Evidence:** healthcheck `WARN universe_size: universe_membership=30656 (expected ~11336), drift=+19320 (170%)
— confirm intentional`. This has been sitting as an unconfirmed WARN.
**Why:** A 2.7× universe blow-up silently changes capture breadth, parity-sweep cost, and trust-grade
denominators. "Confirm intentional" has not been done. Could be duplicate membership rows or a backfill that
over-inserted.
**Exact next action:** query `universe_membership` for distinct symbols vs rows + date range; confirm whether
intentional (broad backfill) or a dup-insert bug; clear the WARN by codifying the expected count.
**Owner:** DataIntegrity.

### G12 — 248 features graded DIVERGENT, persisting as a WARN with no owner-action loop
**Evidence:** `WARN trust_grades: ... {'divergent': 248, 'validating': 504}; 248 DIVERGENT`. Memory
`project-trust-bottleneck-stale-defects` says DIVERGENT is largely STALE pre-fix defects that an UPSERT-only
ledger never auto-clears — but 248 still show, gating the Monday trust jump.
**Why:** The trust % is the headline progress metric (axis A). 248 stale-or-real divergents block ~475 features
from earning trust. We don't currently distinguish stale-defect from live-divergence in the live readout.
**Exact next action:** run the clean re-sweep / auto-close wired in #124/#129/#131 against the latest clean
capture day and re-grade; report stale-vs-live split.
**Owner:** DataIntegrity (gated on a clean capture day — TaskCreate #68).

---

## BUS / DECOUPLING

### G13 — bus name-decoupling (#211 `FeatureView.to_model_vector`) is imported but its decoupling claim is unexercised end-to-end
**Evidence:** `FeatureView` is imported in reversion/smoke strategies and `quantlib/bus/view.py` defines it,
but no running strategy is observed calling `to_model_vector(expected_names)` against a **fingerprint-mismatched**
frame — the whole point of #211 (additions non-breaking, resolve-by-name). The crypto bus
(`fv:crypto:*`) exists but its consumer (crypto-momentum) isn't running (G6), so the cross-fp decouple has
never been driven live. READINESS: "BUILT, NOT EXERCISED-E2E — the gap Ben caught."
**Why:** Until exercised, the next additive feature deploy could still break a strategy (the exact coupling
#211 was built to kill, memory `project-bus-feature-access-decouple`).
**Exact next action:** drive it via the crypto dry-run (G6) OR a deliberate additive-feature deploy where the
strategy's compiled schema is a strict subset of the frame's schema, and assert `to_model_vector` resolves by
name without a rebuild.
**Owner:** CryptoStrategy.

---

## SUMMARY — ranked

| # | Gap | Impact | Owner |
|---|-----|--------|-------|
| G1 | CD daemon not armed; PRs ungraded; self-deploy is fiction | **CRITICAL** | Lead → ContinuousDeploy |
| G4 | Strategies stuck/abandoned; stale orders pollute paper acct | **CRITICAL** | StrategyHealth (new) |
| G9 | No alerting — 5 healthchecks failing all day, nobody paged | **HIGH** | Observability (new) |
| G10 | Healthcheck not market-day-aware → weekend false-FAILs | **HIGH** | Maintainer |
| G2 | WDPC hot-swap not wired into live fc | HIGH | CryptoE2E → Lead |
| G5 | overnight-beta 173% CPU / 96% mem, no trades | HIGH | StrategyHealth |
| G6 | crypto-momentum strategy never launched (no container) | MED | CryptoStrategy → Lead |
| G8 | `ittest` test orders polluting live paper account | MED | Lead |
| G7 | #214 sim==paper proof is opt-in, not a standing gate | MED | Lead |
| G13 | bus name-decouple (#211) never exercised e2e | MED | CryptoStrategy |
| G11 | universe drift +170% unexplained (WARN only) | MED | DataIntegrity |
| G12 | 248 DIVERGENT features, stale-vs-live unsplit | MED | DataIntegrity |
| G3 | latency cron not wired; latency unobservable live | LOW-MED | Lead |

**The one that matters most: G1.** Everything else is a known hole with a known owner; G1 is the keystone Ben
funded that is **built but completely dark** — no daemon, no grades, no auto-merge, no auto-deploy. Until a
supervised `ci_watcher` daemon is running, "a subagent fixes a feature and it reaches prod" cannot happen, and
PRs (like #361 right now) just sit. Arm Phase-1 grade-only first, soak, then Phase-2/3.
