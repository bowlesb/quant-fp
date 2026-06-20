# Feature-store provenance — stream vs backfill vs sim

The authoritative answer to "how do I tell whether a feature-store cell was captured **live**
(provisional) or **backfilled** (settled)?" — the question every coverage surface, the parity
sweep, and any training read has to answer. This note consolidates the truth that today lives
spread across `quantlib/features/store.py`, the parity/sweep code, and the dashboard read-side, so
a consumer doesn't have to reverse-engineer it. It is descriptive (no schema change is proposed);
all line references are to `origin/main`.

## TL;DR

The `source=` path component IS the provenance. There are exactly three values:

| `source=` | Meaning | When written | Settled? |
|-----------|---------|--------------|----------|
| `stream`   | provisional values the **real** running system computed live (per-minute) | a `mode='real'` capture run | NO — provisional until backfilled |
| `sim`      | provisional values a **mock/sim** run computed live | a `mode='mock'` run | NO — mode-separated, never mixed with real |
| `backfill` | settled values recomputed from the historical tape (truth, ~T+1) | the backfill / sweep job | YES — train-eligible |

There is no per-row provenance flag and none is needed: provenance is the partition. A given
`(group, version, date)` can have a `source=stream` partition, a `source=backfill` partition,
both, or neither — and that 2-way presence is the entire "stream vs backfill" coverage axis.

## Physical layout (FEATURE_PLATFORM.md §3.3.1)

```
<root>/group=<g>/v=<ver>/source=<stream|sim|backfill>/date=<YYYY-MM-DD>/data*.parquet
```

`store.py` builds exactly this path in `_partition_dir` (`store.py:49`). To read provenance off
disk you never open a parquet — the `source=` directory name is sufficient.

### File-naming within a partition also encodes the write path

The leaf file names distinguish how a partition was produced (`write_group`, `store.py:88`):

| File name pattern | Producer |
|-------------------|----------|
| `data-<shard>-<epoch>.parquet` | **streaming append** — one file PER MINUTE per shard (`minute=` set). Thousands per `source=stream` partition. |
| `data-<chunk>.parquet`         | a **sharded/chunked backfill** write (concurrent partition-disjoint chunks). |
| `data.parquet`                 | a single un-sharded backfill / repair write. |
| `.tmp-…`                       | an in-flight atomic write; never matches the `data*.parquet` read glob, even mid-write. |

So a `source=stream` partition is recognizable not just by its path but by its characteristic
per-minute file fan-out (the small-file inflation documented in `docs/STREAM_COMPACTION.md`),
whereas a settled partition is one (or a few chunk) files.

## The store mode marker: real vs mock separation

A `_store_mode` marker file at the store root (`store.py:32`, `set_mode`/`store_mode`) tags the
WHOLE root as `"real"` or `"mock"` and **refuses to mix** them. This is why a single root never
holds both `source=stream` and `source=sim`:

- a `mode='real'` run writes its provisional values under `source=stream`;
- a `mode='mock'` run writes under `source=sim` (`source_for_mode`, `_MODE_SOURCE`, `store.py:37`).

So in the live real store (`fp_store_real`), the provisional source is always `stream`; `sim` only
appears in a mock store. The mock separation lets a simulation exercise the EXACT real write/read
path without ever polluting real provisional partitions.

## Read-side resolution: `get_features(source=…)`

`get_features` (`store.py:200`) resolves provenance for a read:

- `source="stream"` / `source="backfill"` (/ `"sim"`) — read ONLY that source. The parity sweep
  uses these two explicitly to diff live vs settled (`parity.py:43-44`,
  `validation_sweep.py:283-284`).
- `source="auto"` (default) — return **backfill where it exists, else the provisional source** for
  the remaining (recent, unsettled) window. The provisional source is chosen by store mode:
  `"sim"` for a mock root, else `"stream"` (`store.py:221`). The merge is: take all settled rows,
  then anti-join the provisional rows to append only the `(symbol, minute)` keys backfill doesn't
  yet have (`store.py:236-242`). This is the train/serve continuum: backfill is truth, stream fills
  the not-yet-settled tail.
- `require_settled=True` (use for TRAINING reads) RAISES if any requested date is provisional-only
  (`store.py:223-231`) — a model never silently trains on unsettled stream values.

Implication for a consumer: **never treat an `auto` read as a provenance signal.** `auto`
deliberately blends the two. To know whether a specific cell is settled, read `source="backfill"`
and `source="stream"` separately and compare presence (which is exactly what the sweep and the
coverage surfaces do).

## The latest-write dedupe — a provisional-side correctness rule

`source=stream` partitions can legitimately contain DUPLICATE `(symbol, minute)` rows: a
fragmented-gather restart splits one minute across several concurrent partial gathers, each writing
a shard file with a DIFFERENT (less/more complete) value. `_scan_source` carries each row's source
file (`include_file_paths`, `store.py:191`) and `_dedupe_latest_write` (`store.py:150`) collapses
duplicates to the LATEST-mtime file's row (last write = most-complete gather), with the file path as
the deterministic tiebreaker. It is a no-op on coherent capture.

Provenance consequence: when you read a stream partition's raw files yourself (bypassing
`get_features`), you may see duplicate keys that are NOT real — they are fragmented-gather artifacts.
A correct read must dedupe `(symbol, minute)` keep-latest-write, or use `get_features`, which already
does. This is the read-path fix DataIntegrity shipped in PR #129; the backfill side is single-write
and not affected.

## How the coverage surfaces classify a (group, date) cell

The canonical classification — the "stream vs backfill" axis a coverage grid renders per
`(group, day)` — is `_day_provenance(stream_n, backfill_n)` (`feature_grid.py:930`):

| Class | Condition | Reading |
|-------|-----------|---------|
| `both`          | stream_n > 0 AND backfill_n > 0 | captured live AND settled — parity-checkable |
| `stream_only`   | stream_n > 0, backfill_n == 0   | captured live, NOT yet settled — **cannot be parity-checked / trusted yet** |
| `backfill_only` | stream_n == 0, backfill_n > 0   | settled history with NO live capture that day (weekend, or under-represented LIVE) |
| `absent`        | both 0                          | neither source has the day |

At the SYMBOL level the same split appears as `both` / `backfill_only` / `stream_only`
(`feature_grid.py:660-662`, `757-758`): `backfill_only` symbols are exactly the set
**under-represented LIVE** — present in the full-universe backfill but not captured on the live
stream (the standing FP_TICK_SYMBOLS coverage gap for the order-flow groups).

Helpers a consumer can reuse instead of re-deriving:

- `settled_dates(root, group, version)` (`store.py:141`) — the dates with a settled backfill
  partition (the train-eligible set). This is the authoritative "is this date settled?" answer.
- `stream_symbols_on(root, day, source=…)` (`store.py:251`) — the distinct symbols captured live on
  a day (unions the `symbol` column across every group's partition, column-pruned; pass
  `source="backfill"` for the settled universe). Note it reads each file with its own per-file
  `symbol` projection because groups have heterogeneous schemas — a single multi-file scan over the
  mixed list would reject the column-superset.

## Practical rules for a provenance-aware consumer

1. **Provenance = the `source=` directory.** Read it off the path; you never need to open a parquet
   to know whether a cell is stream, sim, or backfill.
2. **`source="auto"` is a blend, not a provenance signal.** To classify, read `stream` and
   `backfill` separately and compare presence.
3. **Settled-ness is `backfill` presence**, per `(group, version, date)` — use `settled_dates`.
   A `stream_only` date is provisional and must not be trusted/trained-on as truth.
4. **A real store never holds `sim`; a mock store never holds `stream`.** In `fp_store_real` the
   provisional source is always `stream`.
5. **Dedupe `(symbol, minute)` keep-latest-write on raw stream reads** (or just use `get_features`)
   — fragmented-gather duplicates are artifacts, not data.
6. **`backfill_only` symbols are the under-represented-LIVE set** (the FP_TICK_SYMBOLS gap); they are
   not a defect of the backfill, they are a live-capture-breadth gap.

## No schema change proposed

This note is descriptive only. The `source=` partition axis, the `_store_mode` marker, the file
naming, and the latest-write dedupe are the existing, sufficient provenance mechanism; nothing here
recommends altering the on-disk format. The single observation worth the Lead's awareness (already
tracked in `docs/STREAM_COMPACTION.md`, not re-raised here) is that the per-minute stream file
fan-out is what makes a stream partition recognizable AND expensive to scan — provenance and the
compaction question are the same physical fact seen from two angles.
