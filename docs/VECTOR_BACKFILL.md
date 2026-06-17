# Vector Backfill — historical feature-vector materialization design

**Status: DESIGN / GATED.** Do NOT run until the standing validation agent certifies the active feature
set at parity + trust. Materializing vectors from features that have not passed parity would bake defects
into the training set. This document is the design + the infra asks; the trigger to execute is the
validation green light.

Roadmap position (Ben): **parity → trust → VECTOR BACKFILL → model**. This is the third stage: compute all
active features over the 18-month raw history and store the result as a point-in-time, parity-true ML
training dataset.

## What we already have (reuse, do not rebuild)

The materialize path that guarantees parity-by-construction already exists:

- `quantlib/features/materialize.py` — `materialize_from_raw(root, raw_root, day, symbols)` reads the
  already-downloaded `/store/raw` minute bars and writes `source=backfill` partitions through the SAME
  emit chain the live writer uses. Parity is by construction: both live and backfill build a
  `BatchContext(frames=...)` and call `run_group()` / `run_all()` — the compute path does not know or care
  where the frames came from.
- `quantlib/features/raw_loaders.py` — `load_raw_minute_agg(raw_root, day, symbols)` reads
  `/store/raw/bars/symbol=<S>/date=<D>/data.parquet` and returns the canonical `BARS_SCHEMA`
  `(symbol, minute, open, close, high, low, volume)` — byte-identical to the live `backfill_bars` shape.
- `quantlib/features/engine.py` — `run_all(groups, ctx)` joins every *runnable* group into one wide vector.
- `quantlib/features/store.py` — `write_group(root, name, version, source, day, frame)` atomic per-partition
  parquet writes at `group=<g>/v=<ver>/source=<backfill>/date=<d>/data.parquet`.
- `quantlib/features/parity.py` — `parity_stored(root, day)` compares stored stream-vs-backfill; the gate
  that certifies a materialized day is parity-true.
- DB `feature_vectors` table (`db/init/01_schema.sql`) — `(symbol, ts, set_version, vector double[], source,
  computed_at)`, `source='historical'` for backfill, joined to `feature_sets.names[]` and the
  `training_data` labelled view.

## The ONE real gap: tick-derived features are not yet materialized from /store/raw

`materialize_from_raw` currently builds only three frames:

```python
frames = {
    "minute_agg": load_raw_minute_agg(raw_root, day, symbols),  # /store/raw/bars
    "daily":      backfill_daily(day, symbols),                 # Alpaca daily (split-adjusted)
    "reference":  load_reference(),                             # DB asset_metadata + sector_map
}
```

There is **no `trades` / `quotes` frame**, so `runnable(frames)` SKIPS every tick-derived group:
`microstructure_burst`, `trade_flow`, `tick_runlength`, `signed_trade_ratio` (and Ben's W14 activity-burst).
Those groups declare `InputSpec(name="trades", columns=("symbol","ts","price","size"))` and read
`ctx.frame("trades")`; the live tick path feeds that from the `trades_raw` DB table
(`loaders.load_trades_live`), NOT from `/store/raw`. So today a from-raw materialization is BARS-ONLY.

### Fix (small, parity-preserving) — needed before tick features can backfill

1. Add raw tick loaders mirroring `load_raw_minute_agg`:
   - `load_raw_trades(raw_root, day, symbols) -> pl.DataFrame` reading `/store/raw/trades/symbol=<S>/
     date=<D>/data.parquet`, returning the live `TICK_SCHEMA` `(symbol, ts, price, size)` so it is
     byte-identical to `load_trades_live` (the live side of Layer-C tick parity).
   - `load_raw_quotes(raw_root, day, symbols)` likewise for any quote-derived groups (NBBO/OFI).
   - Missing partitions are skipped (mirrors Alpaca/`load_raw_minute_agg` no-data behavior).
2. Add the frames to `materialize_from_raw`:
   ```python
   frames["trades"] = load_raw_trades(raw_root, day, symbols)
   frames["quotes"] = load_raw_quotes(raw_root, day, symbols)
   ```
   `runnable()` then includes the tick groups automatically (no engine change).
3. **Parity gate for the new loaders:** `parity_test_ticks` already compares live `trades_raw` vs Alpaca
   historical ticks. Add a from-`/store/raw` variant (or extend it) so the raw-tick loader is certified
   cell-identical to the live tick capture before any tick-feature vectors are trusted.

This is the gating dependency between the two current jobs: tick features can only be materialized for the
window where `/store/raw` ticks exist. The liquid-tick 63d→378d extension (in flight) is what makes the
tick features computable over the full 18-month window — for the **top-1000 liquid symbols only** (the
disk budget does not allow full-universe ticks at 378d: ~600GB+). So tick-feature vectors are
liquid-1000-deep; bar/daily/reference features are full-universe-deep.

## Scale

| Axis | Count |
|------|-------|
| symbols (bar features) | ~7,682 (full universe at 378d) |
| symbols (tick features) | ~1,000 (liquid, at 378d) — budget-bounded |
| trading days | ~378 (18 months) |
| minutes / day | ~390 RTH (the materialize unit is a (day) → all symbols × minutes) |
| features | ~610 (active set; query `feature_sets`/`REGISTRY.catalog()` for the exact count) |

Rows ≈ symbols × days × minutes. Bar-feature rows ≈ 7,682 × 378 × 390 ≈ **1.1 billion** (symbol, minute)
vectors over the window. Stored as Float32 / UInt8 columnar parquet at `source=backfill`, partitioned by
`(group, version, source, date)` — the existing layout. The partition granularity is one parquet file per
(group, date), so the write is ~610-groups × 378-days fan-out (well-bounded file count) and parallelizes
cleanly across days with no contention (atomic per-partition writes, no global lock).

## Execution shape (when un-gated)

- **Unit of work = one trading DAY** (computes all symbols × minutes × all runnable groups). Days are
  independent → embarrassingly parallel. Resume = skip days whose partitions already exist (same idempotent
  pattern as the raw backfill manifest).
- **Driver:** a thin orchestrator over `materialize_from_raw(root, raw_root, day, symbols)` looping the 378
  days, parallelized across a process pool (one worker per day-batch), MEMORY-CAPPED like the raw backfill
  so it can never threaten the live capture. Reuse `reconcile`-style resume (skip already-written
  partitions).
- **Symbol scoping:** bar/daily groups over the full universe; tick groups over the liquid-1000 (where
  `/store/raw` ticks reach 378d). Cleanest: run two passes — full-universe (bar/daily/ref groups) and
  liquid-1000 (tick groups) — so a missing-tick symbol never produces a half-filled vector.
- **`daily` input caveat:** `backfill_daily` re-fetches split-adjusted daily history from Alpaca per day —
  that is per-day Alpaca load during materialization (NOT served from `/store/raw`). For a 378-day ×
  full-universe run this is a non-trivial number of daily-history calls; consider caching the daily frame
  once per symbol across the window rather than re-fetching per day. Flag for the execution phase.

## Infra asks (flag now, confirm before execution)

1. **Disk for the vector store.** 1.1B bar-feature vectors × ~610 Float32/UInt8 columns is the large
   consumer. Need a sized estimate (a 1-day full-universe materialize → measure partition bytes → ×378) and
   confirmation the vector store lives on the same 2.4TB-free volume or a separate one. **Do this estimate
   first** when un-gated — it determines feasibility, same discipline as the raw budget.
2. **Compute window / container.** A dedicated memory-capped materialize container (like `quant-backfill`),
   `--restart no`, never touching `feature-computer`. 32-core box → day-parallel pool.
3. **The tick-loader + tick-parity additions above** must land + certify before tick-feature vectors are
   materialized. Bar/daily/reference vectors can materialize as soon as the *bar* features are certified.
4. **Active feature-set version pin.** Materialize against ONE pinned `feature_sets` version so the whole
   training set is internally consistent; record the version in the vector partitions.
5. **Label join.** The `training_data` view joins vectors to labels — confirm the label horizon/source is
   defined for the historical window before declaring the training set complete.

## Trigger

Execute only when the standing validation agent (`aac9f8c8`) certifies the active feature set at
parity + trust. Until then this stays design-only. First execution action when un-gated: the 1-day
disk-sizing probe (ask #1), then a single-day full materialize validated by `parity_stored` before fanning
out across the 378-day window.
