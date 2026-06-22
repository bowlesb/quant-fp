# Source-Data Dependency Abstraction

*Task #74 (Ben's idea). Status: core abstraction built + unit-tested (this PR); the real-fetch wiring
delegates to the existing acquire engines; live DB activation is the Lead's gated step.*

## The idea

A feature-backfill job must **never re-download Alpaca source data repeatedly**. If a job wants to
backfill feature group *X* from now back to date *Y*, and *X* derives from Alpaca quotes over `[Y, now]`,
the job must **first ensure those quotes are in the feature store**, then read the source **exclusively
from the store** — never re-fetch from Alpaca.

So we cleanly **separate two stages** that were previously tangled inside each backfill:

1. **Acquire raw INPUTS into the store** — download bars/trades/quotes into `/store/raw` (the existing
   acquire engines), and
2. **Compute the FEATURE from the stored source** — `materialize` / `selective_backfill` reading
   `/store/raw`.

Behind a single reusable abstraction, `ensure_inputs`, called as the **first step of every feature
backfill**.

### Why (the three benefits)

- **(A) Shared source.** Quotes fetched for `quote_spread` today are already present for `liquidity`
  tomorrow. The manifest dedup means a second job over the same horizon downloads *nothing*.
- **(B) The raw tape stays up to date by construction.** Any hole over a backfill's horizon is patched
  *before* the feature compute runs — so the deep tape only ever fills in, never regresses, and a feature
  backfill can't silently compute over a half-present tape.
- **(C) It strengthens parity certification.** The backfill compute path reads the **same stored source**
  the realtime path's aggregates are reproduced from (`raw_loaders` is byte-for-byte the tick aggregation
  `parity_audit` runs). A live-vs-backfill mismatch can therefore never be a "different download"
  artifact — the source bytes are one shared substrate.

## What already exists (we extend, not duplicate)

| Concern | Existing machinery | File:line |
|---|---|---|
| Raw partition layout | `<store>/raw/<bars\|trades\|quotes>/symbol=<S>/date=<D>/data.parquet` | `quantlib/data/raw_store.py:33` (`partition_dir`) |
| Manifest schema | `(tier, symbol, date, rows, bytes, fetched_at)`, append-only parts | `raw_store.py:23` (`MANIFEST_SCHEMA`), `raw_store.py:74` (`manifest_dir`) |
| Manifest load (union legacy + parts) | `load_manifest(store, tier)` | `raw_store.py:81` |
| **Rows-aware "done" / hole logic** | `resumable_done_keys(...)` — settle-window + forced-symbol poison rule | `raw_store.py:109` |
| Settle policy constants | `SETTLE_WINDOW_DAYS=5`, `FORCE_REFETCH_SYMBOLS`, `MIN_SETTLED_TICK_ROWS=100` | `raw_backfill.py:115,122,125` |
| Bars acquire | `fetch_bars_tier(config, symbols, days)` | `raw_backfill.py:384` |
| Trades/quotes acquire | `run_tier_fast(store, tier, symbols, days, ...)` | `fast_backfill.py:257` |
| Pending-unit (hole) detection on resume | `_pending_units(store, tier, symbols, days)` | `fast_backfill.py:155` |
| Feature backfill READS from store (no re-fetch) | `materialize_from_raw*` + `raw_loaders` | `materialize.py:83`, `raw_loaders.py` |
| Group → layer routing (settle lag) | `settle_lag_for_group` → bars/trades/quotes | `within_day_parity.py:9`, `settle_lag.py:46` |
| DB single-writer lock pattern | `within_day_assignment` claim/heartbeat/release/reclaim (PK = scope) | `within_day_assignment.py`, `db/init/14_wdpc_assignment.sql` |
| Container mem/disk guard | `live_monitor.sh` pauses `quant-backfill`-named jobs under pressure | `ops/live_monitor.sh:38` |

The key reuse: **hole detection is `resumable_done_keys`, not a second definition of "done."** An
"ensured" key means *exactly* what a resume key means — so the settle-window poison handling (a recent
0-row stub is re-fetched; a genuinely-thin aged day is never churned) is shared, not re-implemented.

## Design

### (a) A FeatureGroup DECLARES its raw inputs

`InputSpec` already declares the *named frames* a group reads (`minute_agg`, `trades`). What was missing
is the *raw layer* and horizon those frames derive from. We add a `RawLayer` enum (`bars`/`trades`/`quotes`,
values matching the on-disk tier names verbatim) and a `required_raw_layers()` method on `FeatureGroup`:

```python
class RawLayer(str, Enum):
    BARS = "bars"; TRADES = "trades"; QUOTES = "quotes"

class FeatureGroup:
    def required_raw_layers(self) -> frozenset[RawLayer]:
        return _TYPE_RAW_LAYERS.get(self.type, _DEFAULT_RAW_LAYERS)
```

The default **derives from `self.type`** (reusing the same routing idea as `settle_lag_for_group`), so the
50+ existing groups need **zero changes**:

- bar-derived family (`PRICE`/`VOLUME`/`MOMENTUM`/… ) → `{bars}`
- `TRADE_FLOW` → `{bars, trades}` (its per-minute tick columns aggregate from the trades tape on the bar grid)
- `QUOTE_SPREAD` / `MICROSTRUCTURE` → `{bars, trades, quotes}` (the tick-enriched `minute_agg` joins all three)

The set is **inclusive** — a group always needs `bars` for its minute grid; richer layers add to that.
A group whose true requirement differs from its family default **overrides** `required_raw_layers()` — the
declaration lives *with the group*, the same self-declaration pattern as `reduce_buffer_minutes` /
`up_to_date`, not in a backfill-side lookup table.

The **horizon** is not a group property — it's the backfill job's `[start, end]`, passed to
`ensure_inputs`. (Settle lag — how far back from *now* is settled enough to compare — already lives in
`settle_lag_for_group`; ensure_inputs is about *presence*, not *settledness*.)

### (b) `ensure_inputs(symbols, date_range, layers)`

`quantlib/data/source_dependency.py`:

```python
def ensure_inputs(store, layers, symbols, days, agent_id, fetcher,
                  today=None, lock_timeout_s=1800, dry_run=True) -> EnsureReport
```

For each layer (stable order):

1. **Acquire** the per-layer single-writer lock (below). If held by another live job → **skip** the layer
   (recorded in `skipped_locked`); the caller decides whether to wait+retry. No hang; serialization holds.
2. **Detect holes** — `find_holes(store, layer, symbols, days)` = `load_manifest` + `resumable_done_keys`
   with the *same* `SETTLE_WINDOW_DAYS` / `FORCE_REFETCH_SYMBOLS` / `MIN_SETTLED_TICK_ROWS` policy a resume
   uses. Returns the `(symbol, date)` units not safely present.
3. **Fetch only the holes** via `fetcher(layer, hole_symbols, hole_days)`. Manifest dedup → idempotent: a
   second job over the same horizon finds zero holes and fetches nothing.
4. **Release** the lock.

`dry_run` (default) reports holes without a DB lock — a job can preview what *would* be fetched.
Production passes `dry_run=False` with `default_fetcher(store)`, which dispatches `bars → fetch_bars_tier`,
`trades/quotes → run_tier_fast` (the existing engines, already process-pooled + budget-guarded). The heavy
work lives in `fetcher`; `ensure_inputs` is thin orchestration and is **memory-bounded** (it only computes
hole sets).

### (c) Single-writer lock per source scope

`SourceIngestLock` (DB-backed, `db/init/16_source_ingest_lock.sql`) mirrors `within_day_assignment`
exactly: PK = `layer`, `claim` = `INSERT … ON CONFLICT (layer) DO UPDATE … WHERE status<>'active' OR
heartbeat stale`, plus `heartbeat`/`release`/`reclaim_stale`. A dead job's lock times out via heartbeat so
a layer is never stuck forever.

**Why per LAYER, not per symbol-day:** the acquire engines already fan out symbol-days internally across a
process pool; the resource two concurrent writers would actually race is the **append-only manifest**.
Serializing the layer serializes that. Per-symbol-day locking would be thousands of rows of churn for no
extra safety. This also dovetails with the `quant-backfill`-named container guard in `live_monitor.sh` — a
single named ingest container per layer is the unit that guard already protects.

`dry_run=True` is the default (logs intent, no DB write) — the live activation is the Lead's gated step,
identical to `within_day_assignment`.

### (d) The contract a feature-backfill job follows

```python
from quantlib.data.source_dependency import (
    required_layers_for_groups, ensure_inputs, default_fetcher)

layers = required_layers_for_groups(groups_to_compute)      # union of declared layers
report = ensure_inputs(store, layers, symbols, days,        # patch holes FIRST
                        agent_id=job_id, fetcher=default_fetcher(store), dry_run=False)
assert report.all_present                                    # source is now in the store
# ... now materialize_from_raw* reads source EXCLUSIVELY from /store/raw — no Alpaca re-download ...
```

### (e) Parity strengthening (benefit C, concretely)

The realtime path's tick columns are reproduced in backfill by `raw_loaders._tick_minute_columns`, which
is the *same* aggregation `parity_audit` exercises. Because `ensure_inputs` guarantees the backfill reads
that stored tape (rather than a fresh per-job Alpaca pull), the live and backfill sides of a parity compare
are computed over one shared source. A divergence is then attributable to *compute* (the thing parity
certification is testing), never to two different downloads of the same minute — removing a whole class of
false-divergent noise from the within-day certifier.

## What's built in this PR vs. follow-up

**Built + unit-tested** (`tests/test_source_dependency.py`, 14 tests):

- `RawLayer` enum + `FeatureGroup.required_raw_layers()` (default-by-type + override).
- `required_layers_for_groups()` (the backfill resolves its layer union).
- `find_holes()` — real hole detection against the manifest, reusing `resumable_done_keys` (covers
  empty store, partial fill, settle-window poison re-fetch, aged-out skip).
- `ensure_inputs()` orchestration — fetch only holes, share-the-source no-op, idempotent re-run, lock-held
  skip, multi-layer.
- `SourceIngestLock` (claim/heartbeat/release/reclaim) + `db/init/16_source_ingest_lock.sql`.
- `default_fetcher()` wiring to the real acquire engines (`fetch_bars_tier` / `run_tier_fast`).

**Follow-up (clearly marked):**

- Wire `ensure_inputs` as the first step of `selective_backfill` / `materialize` CLI entry points (the
  call site), behind a flag, so feature backfills route through it.
- Run the `default_fetcher` end-to-end against Alpaca on a small horizon under a `quant-backfill`-named,
  memory-capped container (no multi-GB fetch in this PR).
- Lead-gated: flip `SourceIngestLock` to `dry_run=False` (live DB writes) once the schema is applied.
