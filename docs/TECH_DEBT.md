# Tech-Debt Ledger — owned by the Architect (in Production Eng)

A self-evolving system accretes complexity; this ledger keeps it deliberate. The
Architect triages this every wake, and SCHEDULES periodic "rebuild core things"
maintenance instead of letting debt compound silently. Severity: P1 bites soon, P3 later.

| sev | item | why it's debt | rebuild/repay plan |
|-----|------|---------------|--------------------|
| P1 | experimenter ran STALE code → wrong results | no "running==intended" gate before trusting output | STRUCTURAL FIX STAGED 6/12 (#11): git-SHA baked into every image (ARG GIT_SHA) + content-based assert_image_fresh.sh v2 + `make rebuild-all`. Applied at post-close rebuild. |
| P3 | scheduler maybe_refresh_asset_metadata (L137) uses datetime.now(UTC).date() as once/day gate | same UTC-vs-ET class as #13 but BENIGN — asset_metadata is idempotent UPSERT (not date-keyed); worst case one extra refresh in 20:00–24:00 ET | swap to et_session_date() next time scheduler is touched (not worth a dedicated restart) |
| P1 | rebuild = ON CONFLICT DO NOTHING (can't overwrite) | recompute can't replace stale rows (today-panel UTC bug) | switch panel rebuild to DELETE-then-insert |
| P2 | build_feature_store ~4k sequential round-trips/cadence + per-symbol daily-close query | N+1; fine at 30m, won't scale to tighter cadence/universe | batch bar/daily-close loads (ANY(array)); hoist shared queries |
| P2 | trades/quotes only for 10 symbols | blocks universe-wide order-flow features (modeling roadmap) | the Architect's sharded ingestion-tier decision (see JOURNAL) |
| P2 | ETF exclusion is a name-regex stopgap | fragile; may miss/over-match | proper ETF reference list |
| P2 | `signed_vol_z_30` (v1.2.0 OFI) has fat tails (range [-3158,+1234], std 141 vs expected ~[-5,5]) | quantlib `_flow_zscore` (L148) is formula-correct but a 30-min rolling z on BURSTY signed-vol blows up when the prior window is quiet (denominator≈0); not a norm bug, more data won't fix the tail. Misleadingly named, fragile outliers. Found 6/12 by modeller-2 on the OFI plumbing panel. NOT fatal (GBM scale-invariant), NOT a pilot blocker. | clip output (±10) + floor the stdev denominator. Tier-1 quantlib feature-def change → PR with modeller review (threshold chosen off the >10-day trade_agg distribution). NOT a pre-batch hot-patch. |
| P3 | feature_vectors/labels/predictions uncompressed | storage growth at scale | enable compression once panel-rebuild churn settles |
| P3 | experimenter writes host files as root | permission paper-cuts | add user:uid to the service |

## Scheduled core-rebuilds (maintenance windows)
### LIVE EXECUTION STATUS (2026-06-12, started ~12:1x PT per Ben's DO-IT-NOW — no-restart items run during RTH)
- [x] **Step 1-2 #18 DONE LIVE:** corporate_actions table + corporate_actions_pit view created in live DB;
  first CA fetch populated 7205 actions (7133 cash_dividends + 42 forward + 19 reverse splits + 11
  stock_dividends, ~3yr). Dividends verified present. KLAC 10:1 split captured. FINDING: Alpaca has NO
  announcement/declaration date (process_date is POST-ex) → view announcement_date=NULL (anticipation
  features need a real declaration feed / #21). Backfiller tool image rebuilt to get the CA code.
- [x] **Step 3 #17 KLAC bars DONE LIVE + VERIFIED:** 217,732 bars re-fetched (one Adjustment.ALL pass,
  2023-12-01→now). Max day-over-day jump now 1.19×/0.86× (was ~10×) — discontinuity gone. QA re-verify
  requested (gates denylist removal). Momentum-cell recompute: PENDING Modeller's (A) in-place-v1.1.1 vs
  (B) let-v1.1.2-carry-it call (recommended B — don't mutate the pinned verdict panel).
- [x] **#10 v1.2.0 OFI panel DONE LIVE + VERIFIED:** 1516 vectors / 50 names / 3-day stream window
  (set_version v1.2.0, separate from v1.1.x — no clobber). OFI features REAL: 1298/1516 (85.6%)
  non-NaN (NB Postgres NaN=NaN, so detect NaN via `=‘NaN’::float8`), mean ofi_30m -0.0197, sensible
  intraday imbalances. Momentum NaN-degraded at window start (only 3d stream, needs 10d) = the
  PLUMBING-GRADE caveat, as labeled. Optional follow-up: register v1.2.0 in feature_sets (not needed
  for Modeller to query by set_version).
- [x] **#11 BLOCKING freshness gate DONE:** `scripts/run_tool.sh` (wraps every tools-profile run —
  CONTENT-STALE→rebuild+block, -dirty→warn+run) + rebuild-batch asserts running==intended after up -d.
  Run tools via `scripts/run_tool.sh <svc> <args>` or `make run-tool S=<svc> A="<args>"` so a stale
  image can't be hand-run again (the 4th near-miss). Manager design-call pending: auto-rebuild (chosen)
  vs hard-fail.
- [x] **KLAC denylist condition MET:** QA no_extreme_backfill_jump GREEN + #17 verified + #18 CA live.
  Signaled exec to lift post-deploy (KLAC stays excluded via the data-driven ex-date guard); Manager ratifies.
- [ ] **#12 backfill — at the close (CONCRETE reason):** 222 thin names = sustained paginated data-API
  load that contends with the live executor NBBO during RTH. Run AT CLOSE via the gate:
  `BACKFILL_SYMBOLS=<thin list> BACKFILL_START=2023-12-01 scripts/run_tool.sh backfiller backfill-bars`.
- [ ] Remaining (still gated on close): rebuild-batch (ingestor restart) + executor deploy (exec, lifts KLAC denylist).

### POST-CLOSE 6/12 RUNBOOK (~13:00 PT / 16:00 ET) — turnkey; ONE ingestor restart total
**STEP 0 — FIRST, immediately after BOOK FLAT, BEFORE everything else (DB restart):** raise
`max_locks_per_transaction` 64 → 2048. ROOT CAUSE: 64×100conn = 6400 lock slots; a full-history join
over feature_vectors(614)+labels(613)+bars_1m(693) chunks ≈ 1920 chunks ×~3 locks ≈ 5760 ≈ ceiling →
today's 3 blocked queries + the battery OOM. 2048 → 204,800 slots (~60MB shm, within the 2gb shm_size),
32× headroom for the 6yr chunk-growth trajectory. SEQUENCE: (1) exec confirms BOOK FLAT; (2) tell
modeller "pause the grind" (pause authority); (3) exec HOLDS the executor rebuild; (4) `docker compose
exec -T timescaledb psql -U quant -d quant -c "ALTER SYSTEM SET max_locks_per_transaction = 2048;"` then
`docker compose restart timescaledb`; (5) VERIFY: `SHOW max_locks_per_transaction` = 2048, timescaledb
extension loaded, services reconnected, ingestion resumes (post-close, no bar loss), and ONE previously-
blocked full-history query COMPLETES (e.g. the feature_vectors⨝labels full join). **CRITICAL post-restart
check (learned 6/12): `docker compose ps` — confirm ALL services are Up, not just the DB.** A targeted
`docker compose restart timescaledb` can SIGKILL a peer container, and if that peer was explicitly
`docker compose stop`'ed (e.g. modeller pausing the experimenter), `restart: unless-stopped` will NOT
bring it back — it sits silently DOWN (the experimenter was idle ~10 min this way). Verify Up; `up -d`
any that aren't. Don't trust a recent log tail as "alive" — it can be stale from before the kill. Then proceed to the
rest of the batch; modeller resumes grind; exec proceeds. IaC follow-up: add `command: postgres -c
max_locks_per_transaction=2048` to the timescaledb compose service via a reviewed Tier-1 PR tomorrow
(ALTER SYSTEM already persists it in postgresql.auto.conf in the data volume).
**DONE + VERIFIED (2026-06-12 ~15:52 ET):** restarted timescaledb; SHOW max_locks_per_transaction=2048,
timescaledb 2.27.2 loaded, all services up, experimenter reconnected + queue RESUMED (C11 grind running
post-restart — never-idle holds). EVIDENCE (before→after): a full feature_vectors⨝labels join (20,265,791
rows / 785 syms / 1227 chunks locked) — this class was BLOCKED at 64 (out-of-shared-memory / max_locks) —
now COMPLETES in **10.02s**. IaC compose `command:` follow-up = Tier-1 PR tomorrow.

Prereqs before starting: market closed (≥16:00 ET); Manager go on #12; **THE GUN = exec's
broker-CONFIRMED-flat signal** after the ~15:48 ET EOD-flatten (exec reports 0 positions / 0 open
orders from a FRESH broker snapshot). Do NOT run step 1 before that signal. If exec's flatten report
shows ANY stranded position, the batch HOLDS until it's resolved — a mid-batch ingestor restart with
an open book is the compound failure we don't risk. Sequence (any successor can run this):
1. **Apply #18 DDL** (idempotent, instant): `docker compose exec -T timescaledb psql -U quant -d quant -f /dev/stdin < db/init/05_corporate_actions.sql` (or paste the CREATE TABLE). Verify table exists.
2. **#18 first CA fetch** (cheap): `BACKFILL_SYMBOLS=universe docker compose --profile tools run --rm backfiller fetch-corporate-actions` → confirms KLAC forward_split ex-6/12 lands; note new-action symbols. **VERIFY DIVIDENDS landed too, not just splits** (Ben/Modeller want cash_dividends for overnight-label ideas): `psql -c "SELECT action_type, count(*) FROM corporate_actions GROUP BY 1"` should show cash_dividends rows alongside forward/reverse_splits. **Then PING execution-risk** — table is created+populated; they targeted-rebuild the executor to pick up quantlib.corporate_actions + verify KLAC auto-excludes via the ex-date guard (their guard fails-open until now, so this is the activation point). (DDL create + this populate are adjacent; exec only acts after this ping, so they never see exists-but-empty.) **Also: verify the `corporate_actions_pit` view returns rows + INSPECT a raw payload for the real announcement-date field name (declaration_date vs process_date vs other) and CREATE OR REPLACE the view if needed so announcement_date populates; then PING modeller-2 that corporate_actions + the PIT view are queryable for Family A (ex-div features).**
3. **#17 KLAC re-fetch** (one consistent Adjustment.ALL pass): `BACKFILL_SYMBOLS=KLAC BACKFILL_START=2023-12-01 docker compose --profile tools run --rm backfiller backfill-bars` → then recompute KLAC v1.1.1 momentum cells. Verify no >3× internal jump (QA invariant should flip green). Tell QA + execution-risk (denylist-removal GATED on QA parity green).
4. **#12 Part A** (optional, if Manager go): one-shot deepen the 222 thin universe names (BACKFILL_START=2023-12-01) — see docs/BACKFILL_SCOPE.md.
5. **#16 = STAGING ONLY — NO live swap** (Manager ruling 2b9cde3): v1.1.1 is 21-feat vs the live
   18-feat v1.0.0 contract; a live swap needs a contract bump + replay-equiv re-verify for a no-edge
   hygiene model — deferred to v1.2.0/OFI post-M2 with three-way sign-off. My only #16 action: rebuild
   the trainer image (so Modeller's guarded clean code trains, not the stale image), then Modeller
   trains to a STAGING path. HAZARD: the staging artifact must NOT be models/model_fwd_30m.txt (the
   LIVE model-server path) — a 21-feat file there breaks live 18-feat scoring on reload. Train to a
   versioned path: `MODEL_FILENAME=model_fwd_30m_v1.1.1.txt FEATURE_SET_VERSION=v1.1.1 docker compose
   --profile tools run --rm trainer fwd_30m` (MODEL_FILENAME override added in 4b6b7fe so the staging
   train can't clobber the live model_fwd_30m.txt). model-server stays on v1.0.0; QA v1.0.0 purge stays deferred.
6. **PRE-BUILD: `git status` must be CLEAN** (all peers committed) — a dirty tree bakes `<sha>-dirty`
   into every image and assert_image_fresh.sh flags DIRTY, defeating the #11 provenance. If a peer has
   WIP, get them to commit first (or knowingly accept -dirty). Then **`make rebuild-batch`** (NOT rebuild-all) — GIT_SHA-stamp + restart all long-running BUILT services
   EXCEPT the executor (BATCH_SERVICES = ingestor scheduler feature-computer model-server backfill-manager
   experimenter dashboard). ONE ingestor restart; picks up clean bar-subscription membership + clears the
   benign ingestor quantlib-drift. **Executor EXCLUDED on purpose:** #19 is on master (the b856aa7
   absorption) but needs qa-2 review + Manager bless before deploy — rebuild-all would ship un-approved
   #19. execution-risk owns the executor via a single targeted `make rebuild S=executor` AFTER #19 is
   approved+blessed (that one restart folds in the #18 ex-date guard + #19), keeping the ingestor at
   exactly one restart.
7. **Verify** (running==intended, not just "it restarted"):
   - `scripts/assert_image_fresh.sh` → every service "fresh ... baked <sha>".
   - ingestion resumes fresh (last stream bar within tolerance); model-server scores on next cadence.
   - **BAR SUBSCRIPTION SWAPPED to clean membership** (Manager-added check): ingestor startup log
     shows the expected clean symbol COUNT, AND spot-check that known ETFs (SPY, TQQQ, UVXY) are
     ABSENT from the subscribed list — `docker compose logs ingestor | grep -iE "subscrib|symbols"`
     then grep the subscribed set for SPY/TQQQ/UVXY (must be empty). "It restarted" ≠ "it subscribed
     to the clean list."
   - NB the executor is NOT in rebuild-batch — execution-risk deploys it separately (targeted
     `make rebuild S=executor`) after #19 approval+bless; their guard fails-open until the CA table
     exists, so no rush. Never run `make rebuild-all` tonight (would double-restart the ingestor AND
     ship un-approved #19).
8. **#10 v1.2.0 OFI panel over the 52 order-flow names** (Ben promoted into tonight's batch; sequence
   AFTER #17 KLAC re-fetch + rebuild-batch, before/parallel with #12B grind). PLUMBING-GRADE only
   (~2.5d of capture) — clearly label NOT pilot-grade; purpose = let the Modeller validate the OFI
   experiment pipeline end-to-end over the weekend so the real pilot (~6/26) has zero pipeline risk.
   OFI features already coded (v1.2.0, commit b214dbf). TODO at run time: confirm the exact build
   invocation (FEATURE_SET_VERSION=v1.2.0 over the 52 captured trade/quote names) + register the set.
9. **#12 Part B** (gated on Modeller; AFTER #16 staging train completes): rebuild the full 1000-name
   panel as a NEW version **v1.1.2** — do NOT rebuild v1.1.1 in place (the M1 verdict + #16 reference
   the pinned v1.1.1 panel: 5.5M rows / 785 syms / computed_at 06:43Z). Monthly-chunked + labels + QA
   re-validate. v1.1.2 becomes the research==production panel; v1.1.1 stays frozen for provenance.
   VERIFIED 6/12: the current 1000-name universe is clean (0 ETF/fund-named, 0 missing-meta), so
   v1.1.2 is clean-equities at full depth (NOT a re-contamination) — the extra ~215 vs v1.1.1's 785
   are thin-history equities now backfilled. Register v1.1.2 with explicit composition metadata
   (clean equities / full 1000-name universe / 0 funds / build+source-universe dates).
NB: the backfill-manager CA fetch + recent-split full-history re-fetch trigger is already CODED
(commit 5f17db9) and activates automatically at step 6's rebuild-all — its first non-market-hours
cycle self-populates corporate_actions and re-fetches any recent-split name. The explicit
`fetch-corporate-actions` in step 2 just front-runs that for an immediate populate. Remaining #18
consumers are in other lanes: QA jump-invariant + executor candidate_pool guard (signature handed
to exec), plus a QA unit test for the parser.

## #20 sector_map — deferred state (post-batch + FMP key)
Schema `sector_map(symbol PK, sector, industry, source, updated_at)` staged (db/init/06_sector_map.sql).
Source = FMP /profile (label taxonomy, not strict GICS — categorical grouping suffices; documented in
the DDL). BLOCKED on `FMP_API_KEY` reaching quant .env (lives only in the legacy project's encrypted
secrets; ask relayed to Ben). When the key lands, build (post-batch): `quantlib/sector.py` (FMP fetch
via stdlib urllib, chunked, NULL-safe) + backfiller `fetch-sectors` tool + weekly scheduler refresh.
AT POPULATE TIME: ping Modeller-2 the DISTINCT sector-label SET (not just the null rate) — they want to
eyeball ~11 coherent buckets, no "N/A"/"" pseudo-sector fragmenting the demean groups; and ping QA to
add the <5% null-sector coverage invariant. Consumer = Modeller's v1.3.0 sector-neutral momentum
(JOIN by symbol at compute time, NOT a feature_vectors column).

## Architectural coupling — #11 freshness gate ↔ PR flow (Manager condition, 2026-06-12)
`scripts/run_tool.sh` AUTO-REBUILDS a content-stale tool image to HEAD before running (accepted over
hard-fail: hard-fail reinstates the human-vigilance dependency we're removing; auto-rebuild guarantees
stale code cannot execute AND completes the task). **SAFETY COUPLING:** auto-rebuilding to HEAD is only
safe if HEAD == reviewed code. Once the Tier-1 PR flow binds (tomorrow), master == reviewed, so the gate
can't pull unvetted changes into a run. Until then (and any time direct-to-master is used), a tool run
could rebuild to un-reviewed HEAD. Mitigations in place: the gate logs old→new SHA loudly (provenance
never silent), and tolerates -dirty as WARN+run (doesn't mask uncommitted WIP). If the PR flow ever
lapses, revisit whether the gate should pin to a known-good tag instead of HEAD.

## Cross-agent reviews (REVIEW_POLICY)
- **2026-06-12 — #19 executor (the diff my `git add -A` absorbed into b856aa7). My lane =
  schema/runtime (qa-2 owns the reconcile/fill_reconciliation CONTRACT). VERDICT: APPROVE, no
  blocking findings.** Verified the two real risks and cleared both:
  1. RUNTIME — the absorbed hunk *calls* sync_orders_and_fills / TERMINAL_ORDER_STATES /
     GetOrdersRequest / QueryOrderStatus / dtime; confirmed ALL are defined/imported in current
     executor/main.py (def L292, L69, imports L19/27/28). `git add -A` captured a COMPLETE executor,
     not a half-written WIP — no NameError/crash on rebuild. The cycle except-clause is appropriately
     broad for transient broker/DB errors.
  2. SCHEMA — reconcile reads `COALESCE(filled_qty,0)` but the LIVE orders_log lacks filled_qty
     (01_schema.sql L186 is fresh-init only). NOT blocking: the executor's idempotent EXEC_DDL list
     (L121, executed at startup L392 BEFORE the first reconcile) includes
     `ALTER TABLE orders_log ADD COLUMN IF NOT EXISTS filled_qty numeric` (L127) — the same proven
     self-heal that put the live nbbo_* columns there. So filled_qty lands automatically at the #19
     executor rebuild, before it's read. No manual ALTER needed.
  Also confirmed b856aa7 did NOT touch db/init/01_schema.sql — my #20 (06_sector_map.sql) and exec's
  orders_log work are independent files; a fresh DB init runs both cleanly. Deploy rides the one
  post-flatten executor rebuild (with the #18 guard). Approval to be blessed by Manager after qa-2's
  contract review.
- LESSON (my fault): NEVER `git add -A` in the shared worktree — it absorbed exec's WIP. Switched to
  explicit-path staging per the REVIEW_POLICY patch.

## Incident log (running==intended)
- **2026-06-12: STALE-IMAGE re-contamination caught.** The first M1 clean-universe rebuild ran
  on a STALE `quant-backfiller` image (built 06-11 14:08 PDT, ~6.5h BEFORE the is_etf_like fix
  814e548 @20:44 PDT). `docker compose run backfiller` bakes source into the image (no volume
  mount), so the rebuild used pre-fix `select_universe` and re-produced ~175 ETFs/date. Caught by
  verifying already-rebuilt early dates (2024-01-02 still had iShares/SPDR/ProShares) BEFORE
  trusting completion. Also found `quant-scheduler` stale (06-10) — the LIVE daily universe builder
  would have re-contaminated `universe_membership` on its next pre-open run. FIX: rebuilt both
  images, verified is_etf_like in-image (SPDR/iShares→excluded, TQQQ dropped despite higher ADV),
  re-ran. LESSON: ANY `docker compose run <svc>` after a code edit needs `docker compose build <svc>`
  FIRST — same for restarting long-running services. **GAP: no automated build-freshness gate.**
  Proposed: a `scripts/assert_image_fresh.sh` (image-created-at >= last commit touching its source)
  wired into the rebuild path + CI. Until then, manual image-age check before every pipeline run.
- **2026-06-12: #11 STRUCTURAL FIX STAGED (git-SHA baked into images).** Verifying #13 surfaced the
  timestamp check's fragility live: the scheduler image was built 00:10:03 PT — 36s BEFORE its own
  commit 00:10:39 PT — yet CONTAINS the committed fix (build-then-commit ordering). A clock-based
  guard calls that STALE (false positive); only a CONTENT check is trustworthy. FIX: every Dockerfile
  now declares `ARG GIT_SHA` + `ENV GIT_SHA` (placed after the source COPY so it never busts the
  pip-install layer); `make rebuild`/`rebuild-all`/`build-fresh` inject `--build-arg GIT_SHA=$(git
  rev-parse --short HEAD)[-dirty]`; assert_image_fresh.sh v2 reads the baked SHA from the RUNNING
  container (true running==intended), rejects `-dirty`/unknown SHAs, and asserts the baked SHA
  CONTAINS the last commit touching the service source (git merge-base --is-ancestor) — falling back
  to the old timestamp check only for legacy images with no SHA. Validated: dirty-detection + ancestry
  + legacy fallback paths. Applied to live images at the post-close rebuild-all.
  NB found while staging: ingestor image (built 06-11 12:50) predates 3 quantlib commits (OFI
  b214dbf, is_etf_like→select_universe 814e548, v1.1.1 set 6eb5084) — but NONE touch the ingestor's
  runtime path (it does aggregation + reads pre-built universe_membership; it never calls
  select_universe or computes features), so no live correctness issue; rebuild-all clears the flag.

| P1 | build_feature_store is O(n²) in per-symbol bars | rebuilds close_by_ts/vol_by_ts from the growing bars[:i+1] per cadence point — fine at 51 days, ~36-55h over 600 days (stuck the deep rebuild) | proper fix: precompute close_by_ts/vol_by_ts ONCE per symbol + pass past-only views (careful re lookahead). WORKAROUND IN USE: rebuild in monthly chunks (bounds per-symbol bars → ~2min/month). |
| P1 | backfill SPLIT-ADJUSTMENT DISCONTINUITY (incremental fetch) | backfill-manager walks month-windows at different wall-clock times; when a split's adjustment data lands MID-backfill, pre-split months (fetched earlier, raw) and post-split months (fetched later, adjusted) land in ONE series → a clean Nx step inside a symbol. Found 2026-06-12: KLAC drops exactly 10× at 2026-06-01 (Alpaca ground-truth 2429 = stream correct, BACKFILL 10×-deflated — REVERSES QA's "stream feed error" read). Sweep of 785 names: 11 with >3× day-jumps (KLAC the clean artifact; rest mostly real small-cap/biotech moves). BLAST RADIUS: momentum features only (mom_* span multi-day daily_closes), ~10 days/symbol, ~0.03% of cells — fwd & overnight labels and intraday features unaffected. M1 panel = CAVEAT not invalidate. | (a) re-fetch full history per flagged symbol in ONE consistent Adjustment.ALL pass, then rebuild its v1.1.1 momentum cells; (b) backfill-manager should detect adjustment-version change (or always re-fetch a symbol's WHOLE history when any window changes) so months never mix states; (c) QA invariant: no >3× unexplained day-over-day backfill close jump (fail-loud); (d) LIVE path: model-server mixes correct STREAM intraday with deflated BACKFILL daily_closes → KLAC live score garbage (957/993, denylisted) — needs consistent adjustment basis live. |
