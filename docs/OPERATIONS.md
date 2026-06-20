# Operations Runbook

Operating instructions for the live trading/feature platform. Read this before touching the live
feature-computer or any cron. Companion: the Data Integrity Agent charter (`~/.quant-ops/
data-integrity-agent-charter.md`) and ledger.

## 1. Live capture (feature-computer) — the GOLDEN RULE

**`feature-computer`'s session date is a HARDCODED launch argument** (`python -m
quantlib.features.live_capture <YYYY-MM-DD> /store`). The container does NOT roll the day on its own.

- **To start a new session, or to RECOVER a broken/stale fc: ALWAYS use
  `ops/nightly_relaunch.sh <YYYY-MM-DD>`.** It `docker rm -f`'s the old container (cleanly releasing its
  Alpaca websocket), reseeds the universe for the date, and recreates fc with `FP_WARM_START=1` so features
  are parity-clean from minute one.
- **NEVER `docker restart` / `docker start` feature-computer to "fix" it.** That resurrects YESTERDAY's
  container — wrong session date, and it re-enters whatever broken state it was in. This is the mistake that
  caused the 2026-06-17 incident below.

### Capture health — "Up" does NOT mean "capturing"

The container can be `State.Status=running` (RestartCount=0) while its INTERNAL python capture process
crash-loops and ingests nothing. Container-level monitors (`live_monitor.sh`, which only restarts EXITED
containers) do NOT catch this. To verify capture is actually live, check **data on disk / the bus**, not the
container state or log greps:

```bash
# day fc is anchored to + connection health
docker logs feature-computer --tail 30 | grep -E 'day=|connection limit'
# DEFINITIVE: stream partitions being written for today
docker exec feature-computer bash -c "ls -d /store/group=*/v=*/source=stream/date=$(date +%F) 2>/dev/null | wc -l"
# fresh feature vectors on the bus (latest entry timestamp should be ~now)
docker exec quant-redis redis-cli XREVRANGE fv:AAPL + - COUNT 1
```

## 2. Postmortem — 2026-06-17 capture outage

**What happened:** fc ran all day as the *previous day's* container (launched `...live_capture 2026-06-16
/store`). Its Alpaca SIP websocket dropped overnight; on reconnect the capture process crashed on
`connection limit exceeded` (its own just-dropped socket lingered on Alpaca's side, and Alpaca's SIP feed
allows ONE connection per account), then the entrypoint restarted it into a self-sustaining crash-loop.
Result: 0 partitions captured for 06-17, fc stuck on `day=2026-06-16`.

**Root cause:** there was NO automation running `nightly_relaunch.sh` each morning, so fc was never
recreated for 06-17. Compounding: the operator (me) used `docker restart` to try to fix it, which kept
reviving the same stale container instead of recreating it.

**Fix:** `ops/nightly_relaunch.sh 2026-06-17` — clean recreate cleared the crash-loop and put fc on the
correct day, capturing within minutes.

**Prevention (now in place):**
1. Pre-market relaunch cron (see registry below) recreates fc for the session date every weekday.
2. This runbook's GOLDEN RULE (never `docker restart` fc).
3. TODO (tracked): a freshness check that detects "container Up but not ingesting" and triggers a relaunch
   — closing the monitoring blind spot. `live_monitor.sh` now re-relaunches a MISSING fc (see §3 guardrail).

## 3. Cron safety plan

Crons run unattended and can leave the system worse than they found it (a destructive step that fails, a
storm, a conflict). Every cron MUST follow this; the registry below is the single source of truth.

### Registry — every cron, documented (single source of truth)

**⚠ TIMEZONE: cron times below are SYSTEM LOCAL TIME = America/Los_Angeles (PT).** The crontab has NO
`TZ=`/`CRON_TZ=` override, so every absolute-time cron fires in PT, not UTC. (This column was previously
mislabeled "UTC" — that bug had `nightly_relaunch` firing at 12:11 PT = 15:11 ET, ~49 min before close,
so fc captured almost no RTH on 06-16/06-17. Fixed 2026-06-18 → 05:11 PT pre-market.) RTH = 06:30–13:00 PT
(13:30–20:00 UTC). Verify a cron's real fire time from syslog's `-07:00` stamp, not this label.

| Schedule (PT) | Command | Purpose | Failure mode | Verify |
|---------------|---------|---------|--------------|--------|
| `*/5 * * * *` | `ops/healthcheck.sh --json` | fc freshness/health logging | benign (log only) | `~/.quant-healthcheck/healthcheck.jsonl` |
| `2-59/6 * * * *` | `feature_scan --json` | feature sanity scan | benign | `~/.quant-healthcheck/feature_scan.jsonl` |
| `*/3 * * * *` | `ops/live_monitor.sh` | restart EXITED critical containers; mem/disk guard pauses EVERY non-protected heavy/backfill job (name-pattern, not one hardcoded name) under pressure | conservative (only restarts dead; pauses ONLY non-PROTECTED job-pattern containers — never capture/strategies/store/infra) | `~/.quant-ops/live_monitor.jsonl` |
| `11 5 * * 1-5` | `ops/nightly_relaunch.sh $(date +%F)` | **05:11 PT pre-market clean recreate of fc for the session** (must be before the 06:30 PT open) | DESTRUCTIVE (rm -f fc) — see guardrail | `~/.quant-validation/nightly_relaunch.log` + §1 health check |
| `30 18 * * 1-5` | `ops/daily_lifecycle.sh` | 18:30 PT post-close parity sweep + trust ledger | benign (read/backfill) | `~/.quant-validation/daily_lifecycle.log` |
| `30 23 * * 1-5` | `ops/daily_lifecycle.sh` | **23:30 PT LATE re-acquire + re-sweep.** Alpaca's illiquid-tail SIP historical bars often have NOT settled by 18:30 PT (~5h post-close), so the 18:30 sweep `RawNotSettled`-SKIPs (06-17=65% / 06-18=56% < the 90% `assert_tail_settled` gate) and grades nothing → 0 newly-trusted. By 23:30 PT (~10.5h post-close) the tail has settled; this re-run re-acquires the now-landed tail (idempotent: `raw_backfill` re-fetches the empty 0-row manifest entries left at 18:30) + re-sweeps to grade the day. The direct trust-jump unblock. | benign + idempotent (re-acquire manifest-skips already-on-disk; the sweep upserts — a day already graded at 18:30 just refreshes in place; still `RawNotSettled`-SKIPs cleanly if the tail is somehow <90%). Off RTH, after the 22:33 compaction. | `~/.quant-validation/daily_lifecycle_late.log` |
| `45 14 * * 6` | `ops/trust_random_check.sh` | weekly RANDOM re-check of TRUSTED features on a random recent clean day; un-trusts clean-day failures (docs/TRUST_REDESIGN.md) | conservative (only un-trusts on a positive clean-day disagreement; re-runs an idempotent sweep) | `~/.quant-validation/trust_random_check.log` |
| `3-58/5 * * * *` | `ops/collect_jobs_status.py` | refresh the `/jobs` dashboard's `jobs_status.json` (parse `crontab -l` + each cron's verify-log + `docker ps`) | benign (READ-ONLY: no mutations) | `~/.quant-ops/jobs_status.json` (its mtime = last refresh) |
| `33 22 * * 1-5` | `ops/compact_stream.sh` | 22:33 PT fold each SETTLED stream partition's ~thousands of per-minute files into one `data-compacted.parquet` (docs/STREAM_COMPACTION.md) — runs after the 18:30 sweep, off RTH | benign (idempotent + atomic + reader-transparent; only days `< today`, never the partition fc is writing; NO schema/format/fingerprint change) | `~/.quant-validation/compact_stream.log` |

**Keep this table updated whenever a cron is added/changed/removed.** A cron that isn't here doesn't exist.

The random trust re-check is the safety net behind 1-day trust (docs/TRUST_REDESIGN.md): it re-grades a
random recent clean day and un-trusts any TRUSTED feature that now falls below its per-type threshold,
filing a parity defect. Trust is otherwise PERMANENT per `(feature, version)`; this is the only un-trust
path. Saturday 14:45 PT keeps it off the weekday capture/sweep windows. It was documented here but had
drifted out of the live crontab (hand-managed, no installer); `ops/install_crons.sh` closes that gap
idempotently — it appends this one entry if absent and is a no-op if present, never touching the other
(hand-managed) lines. Run `ops/install_crons.sh --dry-run` to preview, then `ops/install_crons.sh` to
install; verify with `crontab -l | grep trust_random_check`.

### Checklist — before adding or changing ANY cron

1. **Idempotent** — re-running converges (manifest-skip, UPSERT, clean-replace); no duplicate side effects.
2. **Fail-safe** — a failure leaves the system no worse. For DESTRUCTIVE steps (rm/recreate), verify the
   new state is healthy and have a RECOVERY path (see guardrail). Prefer verify-then-swap over
   destroy-then-rebuild where possible.
3. **Dry-run tested** — run the command (or its `--dry-run`) by hand and confirm the outcome BEFORE
   scheduling. `nightly_relaunch.sh <date> --dry-run` changes nothing.
4. **Logged** — append to a dated log under `~/.quant-*` with timestamps; never silent.
5. **Timed safely** — no destructive action during RTH (13:30–20:00 UTC); use an off-:00 minute; stagger
   from other crons so destructive jobs never overlap.
6. **Self-verifying + monitored** — the cron checks its own success (exit non-zero on failure), and a
   SEPARATE monitor confirms it ran and the system is healthy afterward.
7. **Durable & owned** — production crons live in the system `crontab` (survive restarts). Session-scoped
   `CronCreate` jobs are for transient work only and auto-expire after 7 days — never rely on them for
   production. Record the owner here.

### Guardrail — destructive crons need a recovery path

`nightly_relaunch.sh` does `docker rm -f feature-computer` BEFORE recreating. If the recreate fails, fc is
GONE (not merely stopped), and `live_monitor.sh`'s `docker start` cannot bring back a removed container —
capture would be down with no auto-recovery. Mitigation (implemented): **`live_monitor.sh` detects a MISSING
feature-computer (state=`missing`) and re-runs `nightly_relaunch.sh $(date +%F)`** to rebuild it, in
addition to `docker start` for merely-stopped containers. So a failed relaunch self-heals on the next
3-minute monitor tick.

### How the operator/agent runs a cron change safely

1. Edit the registry table above (in this doc) first — describe the cron.
2. `--dry-run` / hand-test the command.
3. Install via `crontab -l | … | crontab -` preserving existing entries; use an off-:00 minute.
4. Verify with `crontab -l` and watch the first real run's log.
5. For destructive crons, confirm the recovery path (guardrail) exists.
