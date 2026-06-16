# Backfill spine — ACQUIRE → MATERIALIZE → VALIDATE

The backfill tooling is a three-stage spine. Each stage has ONE owner module and a thin ops wrapper;
they chain so the raw tape is downloaded once and reused for every recompute and certification.

```
  ACQUIRE                      MATERIALIZE                    VALIDATE
  ───────                      ───────────                    ────────
  Alpaca tape  ──download──>   /store/raw  ──compute──>       features      ──compare──>   trust ledger
  (bars/trades/quotes)         (minute bars)   features         (source=backfill)   vs        (Postgres +
                                                                                  source=stream    parquet)
  quantlib.data.raw_backfill   quantlib.features.materialize  quantlib.features.validate
  ops/raw_backfill.sh          (materialize_from_raw)         (symbol-scoped)
```

A single dispatcher, `ops/backfill.sh <acquire|materialize|validate>`, fronts all three (it only
delegates — the stage modules are unchanged). The dispatcher is the recommended entrypoint; the
per-stage commands below are what it runs and remain directly usable.

---

## Stage 1 — ACQUIRE: download the raw tape once

**Module:** `quantlib.data.raw_backfill` · **Wrapper:** `ops/raw_backfill.sh`

Fetches raw minute **bars** (all universe symbols), raw **trades** (top-`top_trades` liquid), and raw
**quotes** (top-`top_quotes` liquid) from Alpaca and lands them under `/store/raw`, ranked liquid-first
and bounded by an on-disk budget. The job is **resumable + idempotent**: every fetched (tier, symbol,
date) is recorded in an append-only per-tier manifest, so a re-run SKIPS what is already on disk and
RESUMES an interrupted run. Download-once is the whole point — materialize never re-hits the API.

**Layout written:**

```
/store/raw/<bars|trades|quotes>/symbol=<S>/date=<YYYY-MM-DD>/data.parquet   the tape
/store/raw/_manifest_<tier>.d/part-*.parquet                                append-only resume manifest
```

**Run it:**

```bash
ops/backfill.sh acquire sample          # AAPL,SPY,NVDA x 2 recent trading days (evidence; cheap)
ops/backfill.sh acquire full            # 6mo, top-1500 trades / top-300 quotes, ~1.8 TB budget (LONG)

# tier-width overrides pass straight through to ops/raw_backfill.sh:
TOP_TRADES=2000 TOP_QUOTES=400 ops/backfill.sh acquire full
```

Equivalent direct call (what the wrapper runs):

```bash
docker run --rm --network quant_default --env-file .env \
  -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \
  python -m quantlib.data.raw_backfill --store /store --months 6 \
    --top-trades 1500 --top-quotes 300 --budget-tb 1.8
```

The full run is long; a subagent should use `sample` for evidence and let the LEAD kick off `full`.

---

## Stage 2 — MATERIALIZE: compute features from the raw tape

**Module:** `quantlib.features.materialize` (the `raw` subcommand → `materialize_from_raw`)

Reads the already-downloaded `/store/raw` minute bars (NOT re-fetching from Alpaca) and computes every
runnable bar-feature group, writing each to its `source=backfill` partition in the feature store. Daily
history comes from `backfill_daily` and reference data from the DB; the per-cell result is schema- and
value-identical to the prior re-fetch path, so features are **parity-true by construction** regardless
of whether bars came from the raw tape or a live fetch.

**Layout written:**

```
/store/group=<g>/v=<ver>/source=backfill/date=<day>/data.parquet
```

**Run it** (N = the N most liquid symbols for the day):

```bash
ops/backfill.sh materialize 2026-06-15 10            # 10 liquid symbols for 2026-06-15 from /store/raw
ops/backfill.sh materialize 2026-06-15 10 /store     # explicit raw_root (default /store)
```

Equivalent direct call:

```bash
docker run --rm --network quant_default --env-file .env \
  -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \
  python -m quantlib.features.materialize raw /store 2026-06-15 10 /store
#                                          │    │      │          │  └ raw_root (reads <raw_root>/raw)
#                                          │    │      │          └ N liquid symbols
#                                          │    │      └ day
#                                          │    └ feature store root
#                                          └ "raw" subcommand = read /store/raw, do not re-fetch
```

Tick-derived groups (trade-flow, quote-spread, microstructure) are NOT produced here — they need the
trades/quotes tape, not minute bars; this stage covers the bar-feature groups.

---

## Stage 3 — VALIDATE: certify live features against the backfill

**Module:** `quantlib.features.validate`

Compares what was collected LIVE (`source=stream`) against what backfill produced (`source=backfill`)
for a settled day, classifies every cell (match / mismatch / extra_live / missing_live), and writes the
durable **validation ledger** — the per-(feature,day) rollup, the per-feature **trust registration** the
training gate reads, and the rare diverging cells — to Postgres
(`feature_validation_day` / `feature_trust` / `feature_validation_exception`) and parquet.

**Symbol scope (the OOM fix):** pass `--symbols` to validate only a small set. The symbol filter is
pushed into the store reads, so only those partitions load — never the full ~11k-symbol stream root.
Omit `--symbols` for the full-universe run (which must be chunked at scale).

**Run it:**

```bash
# scope to ~10 liquid symbols (well under a few GB); --allow-today permits a closed-session test today
ops/backfill.sh validate 2026-06-15 /store /store/_validation --allow-today \
  --symbols AAPL,MSFT,NVDA,SPY,TSLA,AMD,META,AMZN,GOOGL,JPM

# full-universe certification of a SETTLED (T+1) day:
ops/backfill.sh validate 2026-06-13 /store /store/_validation
```

Equivalent direct call:

```bash
docker run --rm --network quant_default --env-file .env \
  -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \
  python -m quantlib.features.validate 2026-06-15 /store /store/_validation --allow-today \
    --symbols AAPL,MSFT,NVDA,SPY,TSLA,AMD,META,AMZN,GOOGL,JPM
```

`status=certified` means the live compute path reproduces backfill on the RECENT overlap — see
`docs/VALIDATION_LEDGER.md` for the grade thresholds and the honest deep-history scope limit.

---

## How the stages chain

```bash
# 1. acquire the tape once (resumable)
ops/backfill.sh acquire sample

# 2. materialize backfill features for a day from that tape
ops/backfill.sh materialize 2026-06-15 10

# 3. certify the live-collected features against that backfill (scoped)
ops/backfill.sh validate 2026-06-15 /store /store/_validation --allow-today \
  --symbols AAPL,MSFT,NVDA,SPY,TSLA,AMD,META,AMZN,GOOGL,JPM
```

Stage 1 writes `/store/raw`; stage 2 reads `/store/raw` and writes `source=backfill`; stage 3 reads
both `source=backfill` and `source=stream` and writes the trust ledger. Each stage is independently
re-runnable and idempotent.

## Heavy runs

Any heavy materialize/validate run from a subagent should go through `ops/sandbox.sh` (memory/cpu-capped,
prod `/store` mounted read-only) so a runaway job can never OOM live capture. The `full` acquire is the
LEAD's long run, not a subagent's.

## Not part of this spine

`ops/nightly_relaunch.{sh,py}` is a SEPARATE operational concern — it reseeds the universe and relaunches
the live capture container with a warm start for a clean session; it does not acquire/materialize/validate
and is intentionally left out of the dispatcher.
