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
| P3 | feature_vectors/labels/predictions uncompressed | storage growth at scale | enable compression once panel-rebuild churn settles |
| P3 | experimenter writes host files as root | permission paper-cuts | add user:uid to the service |

## Scheduled core-rebuilds (maintenance windows)
### POST-CLOSE 6/12 RUNBOOK (~13:00 PT / 16:00 ET) — turnkey; ONE ingestor restart total
Prereqs before starting: market closed (≥16:00 ET); Modeller battery done (✓ guard satisfied);
Manager go on #12; Modeller GO on #16 swap + #12 panel rebuild. Sequence (any successor can run this):
1. **Apply #18 DDL** (idempotent, instant): `docker compose exec -T timescaledb psql -U quant -d quant -f /dev/stdin < db/init/05_corporate_actions.sql` (or paste the CREATE TABLE). Verify table exists.
2. **#18 first CA fetch** (cheap): `BACKFILL_SYMBOLS=universe docker compose --profile tools run --rm backfiller fetch-corporate-actions` → confirms KLAC forward_split ex-6/12 lands; note new-action symbols.
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
6. **`make rebuild-all`** — FIRST build with GIT_SHA baked into every image (running==intended); ONE ingestor restart; picks up clean bar-subscription membership + clears the benign ingestor quantlib-drift (OFI/is_etf_like/v1.1.1 commits the running ingestor predates).
7. **Verify**: `scripts/assert_image_fresh.sh` → all "fresh ... baked <sha>"; ingestion resumes fresh (last bar within tolerance); model-server scores on next cadence.
8. **#12 Part B** (gated on Modeller): monthly-chunked v1.1.1 panel rebuild over full 1000-name universe (DELETE-then-insert) + labels + QA re-validate.
NB: the backfill-manager CA fetch + recent-split full-history re-fetch trigger is already CODED
(commit 5f17db9) and activates automatically at step 6's rebuild-all — its first non-market-hours
cycle self-populates corporate_actions and re-fetches any recent-split name. The explicit
`fetch-corporate-actions` in step 2 just front-runs that for an immediate populate. Remaining #18
consumers are in other lanes: QA jump-invariant + executor candidate_pool guard (signature handed
to exec), plus a QA unit test for the parser.

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
