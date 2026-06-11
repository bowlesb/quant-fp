# INSPECT — how to query the DB and engage every debugging system

Standard reference for ALL agents (and the manager). Every specialist gets pointed here
so nobody depends on the manager to relay how to inspect the system. All commands run
from the repo root (`/home/ben/quant`).

## Query the database (TimescaleDB / Postgres)

```bash
docker compose exec -T timescaledb psql -U quant -d quant -c "SELECT ...;"
# DB name=quant, user=quant. -T (no TTY) is required for non-interactive/agent use.
# Pipe a whole script: docker compose exec -T timescaledb psql -U quant -d quant < scripts/data_probes.sql
```

### Tables (public schema)
- **bars_1m** — 1-min OHLCV+vwap; `source IN ('stream','backfill')`. The base data.
- **trade_agg_1m / quote_agg_1m** — per-minute microstructure aggregates (subset of symbols).
- **trades_raw** — raw ticks (10-symbol subset; 30-day retention).
- **universe_membership** — point-in-time daily universe (`trade_date, symbol, in_universe`).
- **asset_metadata** — tradability/shortability/name per symbol.
- **feature_sets** — `version, names[]` (the ordered feature contract per version).
- **feature_vectors** — `symbol, ts, set_version, vector double precision[], source` (historical/stream/live).
- **labels** — forward cross-sectional excess returns (`horizon` fwd_30m/fwd_60m).
- **predictions** — model scores (`model_version, ts, score, rank, decile`).
- **orders_log / fills_log / reconciliation_log** — execution + broker reconcile.
- **data_quality_daily** — coverage; **backfill_windows** — backfill ledger.

### Common diagnostic queries
```sql
-- feature sets + sizes
SELECT version, array_length(names,1) FROM feature_sets ORDER BY version;
-- panel coverage by version
SELECT set_version, count(*), count(DISTINCT ts::date) FROM feature_vectors GROUP BY 1;
-- a feature's index in a version (vector[i] <-> names[i], 1-based in SQL)
SELECT i, names[i] FROM feature_sets, generate_subscripts(names,1) i WHERE version='v1.1.0';
-- latest predictions + degeneracy
SELECT model_version, max(ts), count(DISTINCT score) FROM predictions GROUP BY 1;
```

## The probe battery (data-integrity checks)
```bash
docker compose exec -T timescaledb psql -U quant -d quant < scripts/data_probes.sql
```
Includes integrity invariants, calendar-ET correctness, prediction tradeability, and the
per-feature **warmup/coverage** probe (early-vs-late NaN). Add new probes here; QA owns it.

## Service logs / health
```bash
docker compose ps                                  # service status
docker compose logs <service> --tail 50            # e.g. model-server, executor, ingestor
docker stats --no-stream                           # CPU/mem/IO (find DB contention)
```
Services: ingestor, feature-computer, model-server, executor, experimenter, scheduler,
backfill-manager, dashboard, prometheus, grafana, timescaledb.

## Experiments (the Modeller's sandbox)
```bash
cat docs/EXPERIMENTS.md            # human-readable log + findings
cat experiments/results.jsonl      # structured results (IC, NW t, canary, importances)
cat experiments/queue.json         # queued experiments (drop in new ones; experimenter runs them)
docker compose logs experimenter --tail 30
```

## Dashboards
- App dashboard (LAN): http://192.168.1.32:8088
- Grafana (LAN): http://192.168.1.32:3001

## What changed recently / current state
```bash
scripts/team_brief.sh              # commits + diffs since last review + DB feature sets
cat STATE.md                       # current plan; cat docs/QA_LEDGER.md  # standing QA concerns
git log --oneline -15; git diff --stat HEAD~5..HEAD
```

## Run a feature/label build or training (tools profile)
```bash
docker compose --profile tools run --rm -e FEATURE_SET_VERSION=v1.1.0 -e FEATURE_BAR_SOURCE=backfill \
  -e USE_PIT_UNIVERSE=1 -e FEATURE_CADENCE_MIN=30 -e BACKFILL_START=2026-03-30 backfiller build-features
docker compose --profile tools run --rm trainer fwd_30m     # train + report IC/canary
```
