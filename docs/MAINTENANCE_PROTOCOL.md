# Maintenance Protocol — Live Feature Collection

**Purpose:** make "always look, continuously, then fix" a concrete, repeatable engine instead of ad-hoc bash. Every signal that would reveal a problem *if we looked* is encoded as a check that runs on a schedule, classifies itself PASS/WARN/FAIL, and either self-heals (safe fixes) or escalates (everything else) — with a durable record each time.

This document is the protocol. `quantlib/ops/healthcheck.py` + `ops/healthcheck.sh` are its engine.

## The loop

Every cycle (automated + agent-driven):

1. **LOOK** — run the full check registry against the *live* system. No check is skipped; a check that errors becomes a FAIL (fault-isolated, never aborts the run).
2. **CLASSIFY** — each check yields PASS / WARN / FAIL with a real number and a `fix_hint`.
3. **FIX or ESCALATE**
   - **Safe auto-fix** (allowed without asking): restart a *demonstrably dead/crash-looping* container; re-seed `universe_membership` if *empty* for the session. Nothing else.
   - **Escalate** (report at the TOP of the note, do not touch): anything that changes feature values, load, schema, or requires a redeploy; any FAIL whose cause isn't a dead container; any new WARN class.
4. **RECORD** — automated runs append JSONL; agent runs write a timestamped `docs/progress/` note. Healthy = one line. Anything needing a human goes first.
5. **ITERATE** — every genuinely new failure mode becomes a *new check* so the next look catches it automatically. The registry only grows.

## Engine

| Tool | Runs | Scope |
|---|---|---|
| `ops/healthcheck.sh [--fix] [--json]` | host (cron) | docker-level liveness (Up, restart policy, ESTABLISHED :443 to Alpaca, crash-loop delta) + conservative dead-container restart, then execs the python |
| `python -m quantlib.ops.healthcheck [--json] [--session-phase auto]` | inside `feature-computer` | data/value/latency/parity/storage checks (read-only) |

Exit code: `0` no FAIL · `1` any FAIL · `2` engine error. The cron alerts on non-zero.

## Check registry (what could show a problem — so we always look)

**Liveness / freshness**
- `newest_minute_age` — newest stream minute < 3 min (WARN >3, FAIL >8). The single best "are we collecting" signal.
- `worker_targets_up` — all 8 capture shards (`feature-computer:9201–9208`) UP; WARN on any *other* down target (catches stale scrape configs).
- container Up + `restart=unless-stopped` + ESTABLISHED :443 + crash-loop delta (host wrapper).

**Coverage** (the failure mode that bit us — be relentless here)
- `universe_size` — `count(*) universe_membership` vs `EXPECTED_UNIVERSE_SIZE`; WARN on drift (catches a silent re-seed regression *or* confirms a deliberate widening).
- `distinct_symbol_coverage` — distinct symbols with ≥1 bar today / universe, %; phase-aware.
- `per_minute_active` — active symbols in the newest complete minute vs a phase band.
- `alphabetical_bias` — share of today's symbols in A–E; WARN >55% (an unbiased universe is ~30–35%). **This check exists because the night-1 universe was an alphabetical `[:3000]` truncation; it independently re-discovered that from the data.**

**Correctness / value sanity**
- `ohlc_invariants` — no impossible values (negative dollar-volume, close out of [low,high], etc.).
- `nan_rates` — per-feature NaN past warmup; FAIL on any `nan_policy=none` feature with NaN, WARN >20% elsewhere.
- `constant_features` — any feature with a single unique value past warmup (dead/broken).

**Latency** (the bet metric)
- `bar_to_vector_latency` — `feature_vector_latency_seconds` p50/p95/p99 over 15m; WARN p99>0.5s, FAIL >2s. (WARN "not emitted" until the latency build is deployed.)
- `group_compute_p99` — secondary signal.

**Parity / trust** (the crown jewel — read-only)
- `validation_freshness` — `feature_validation_day` not stale > 1 trading day.
- `trust_grades` — `feature_trust` breakdown; WARN if any feature flips to `divergent` or certified coverage drops.

**Infra / storage**
- `disk_space` — `/store` and `/` free space (WARN <15%, FAIL <5%). The host has a known-failing `sda` — this matters.
- `db_growth` — DB reachable, key hypertable row counts growing.

## Cadence

| Cadence | Mechanism | Job |
|---|---|---|
| **5 min** | direct cron → `ops/healthcheck.sh --json` (read-only) → JSONL | cheap, continuous, LLM-free; the always-on tripwire |
| **30 min** | agent checkup cron | runs the engine, interprets, applies safe fixes, writes a `docs/progress/` note |
| **Daily (after close)** | after-market cycle | materialize backfill latest day → validation ledger → refresh trust grades |
| **Weekly** | agent audit | coverage/bias trend, NaN drift, new-feature checklist, registry review |

## Safe-fix policy (what the engine may do unasked)

ALLOWED: restart a dead/exited/crash-looping container (`--fix`, only when demonstrably down); re-seed `universe_membership` when *empty* for the session.

NOT ALLOWED without a human: anything that drops/rewrites data, changes feature values, changes load (e.g. universe widening), redeploys running capture code, or alters schema. These ESCALATE.

## Known-issues register (live)

- **Universe is alphabetically truncated** (`MAX_SYMBOLS=3000`, `[:3000]` cut → ~97% A–E). Fix staged: widen to the full primary-listed set (re-seed, removes bias). Needs one `feature-computer` restart. *Pending deploy decision.*
- **Latency metric not yet emitted on the live feed** — built on the deploy branch; activates on the same restart.
- **Validation ledger hasn't run** — `feature_validation_day`/`feature_trust` empty; trust grades unavailable until the after-market cycle runs.
- **Universe ranking is alphabetical, not liquidity** — replace `[:N]` with ADV-ranking once backfill history accrues (placeholder `adv_dollar=1e6` today).
