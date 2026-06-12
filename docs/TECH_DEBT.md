# Tech-Debt Ledger — owned by the Architect (in Production Eng)

A self-evolving system accretes complexity; this ledger keeps it deliberate. The
Architect triages this every wake, and SCHEDULES periodic "rebuild core things"
maintenance instead of letting debt compound silently. Severity: P1 bites soon, P3 later.

| sev | item | why it's debt | rebuild/repay plan |
|-----|------|---------------|--------------------|
| P1 | experimenter ran STALE code → wrong results | no "running==intended" gate before trusting output | rebuild+restart+verify after edits (Manager duty added); consider a code-version stamp in experiment records |
| P1 | rebuild = ON CONFLICT DO NOTHING (can't overwrite) | recompute can't replace stale rows (today-panel UTC bug) | switch panel rebuild to DELETE-then-insert |
| P2 | build_feature_store ~4k sequential round-trips/cadence + per-symbol daily-close query | N+1; fine at 30m, won't scale to tighter cadence/universe | batch bar/daily-close loads (ANY(array)); hoist shared queries |
| P2 | trades/quotes only for 10 symbols | blocks universe-wide order-flow features (modeling roadmap) | the Architect's sharded ingestion-tier decision (see JOURNAL) |
| P2 | ETF exclusion is a name-regex stopgap | fragile; may miss/over-match | proper ETF reference list |
| P3 | feature_vectors/labels/predictions uncompressed | storage growth at scale | enable compression once panel-rebuild churn settles |
| P3 | experimenter writes host files as root | permission paper-cuts | add user:uid to the service |

## Scheduled core-rebuilds (maintenance windows)
- (none scheduled yet) — Architect proposes one when debt in an area crosses a threshold.

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

| P1 | build_feature_store is O(n²) in per-symbol bars | rebuilds close_by_ts/vol_by_ts from the growing bars[:i+1] per cadence point — fine at 51 days, ~36-55h over 600 days (stuck the deep rebuild) | proper fix: precompute close_by_ts/vol_by_ts ONCE per symbol + pass past-only views (careful re lookahead). WORKAROUND IN USE: rebuild in monthly chunks (bounds per-symbol bars → ~2min/month). |
