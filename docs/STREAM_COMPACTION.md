# Stream parquet small-file problem & compaction

**Status:** REVIEW NOTE (Data Warehouse Manager). No schema/format change is proposed or made here — the
compaction *mechanism* already exists (`quantlib/features/compact.py`, tested). The gap this note documents
is purely **operational: compaction is built but never scheduled**, so settled stream partitions keep their
intraday small-file explosion forever. The fix is a one-line cron hook (proposed below) — but the fc write
format and the reader are unchanged, so **no Lead format approval is required to act on the proposal**;
Lead approval is requested only for the cron-registry addition.

## The measurement (live `fp_store_real`, 2026-06-18)

The live append path writes **one parquet per (shard, minute)** for O(1), crash-safe intraday writes
(`store.write_group(..., minute=)`). That is correct for the hot path but leaves a settled day as a swarm
of tiny files instead of the single compact file a settled partition should be.

Per-partition, a full session day:

| group (one day) | source=stream files | source=backfill files | stream bytes | backfill bytes |
|---|---:|---:|---:|---:|
| trade_flow 2026-06-16 | **10,664** (8 shards x 1,333 min) | 7 | 90.6 MB | 14.7 MB |
| quote_spread 2026-06-16 | 10,664 | 1–7 | — | — |
| price_volume 2026-06-16 | 10,664 | 1–15 | — | — |

System-wide (live store, all 43 stream-writing group partitions per day):

- **One session day (2026-06-16) = 375,522 stream parquet files** across 43 partitions (~8,700 files/partition).
- **Whole stream store ≈ 331,241 files / 2.74 GB, avg 8.3 KB/file (median ~6 KB).** A `find` over the stream
  tree to even *count* the inodes ran for minutes — the inode pressure is already self-evidencing.

So each stream partition is ~10⁴ files averaging ~8 KB where a settled partition wants ~1 file of a few MB.

## Why it matters (read-amplification)

Every reader globs `data*.parquet` and unions the partition (`compact.compact_partition`, the dashboard's
`feature_grid`, the parity `validation_sweep`, the Modeller's panel builds all read the same partitions):

- **Inode / open() cost.** Reading one group-day means ~10,664 `open()`+parquet-footer-parse calls instead
  of 1. parquet's per-file overhead (footer, schema, metadata) dominates at 8 KB/file — the payload is a
  rounding error next to the per-file fixed cost. This is pure read-amplification on **every** sweep, every
  dashboard coverage scan, every backfill-vs-stream parity diff that touches a stream partition.
- **Compression left on the table.** zstd ratio is far worse on 8 KB chunks than on a multi-MB file: the
  observed trade_flow day is **90.6 MB across 10,664 stream files vs 14.7 MB across 7 backfill files for the
  same logical data — a ~6× on-disk inflation** purely from not compacting (the compactor rewrites zstd-19).
- **Backup / volume-snapshot cost** scales with inode count, not bytes — 331k tiny files is the expensive
  axis for any `fp_store_real` snapshot or migration.

## What already exists (don't rebuild)

`quantlib/features/compact.py` is complete and tested (`tests/test_fp_compaction.py`):

- `compact_partition(partition)` — globs `data*.parquet`, unions, **de-dups `(symbol, minute)` keep-last**
  (idempotent over re-delivered minutes — and incidentally a clean fix for the fragmented-gather dup-row
  class the Parity loop flagged on the *read* side), rewrites a single `data-compacted.parquet` at zstd-19.
- **Crash-safe + idempotent:** writes the compacted file via `os.replace` BEFORE unlinking the per-minute
  files, so a mid-compaction crash leaves a correct union a re-run finishes; re-running a compacted
  partition is a no-op.
- `compact_day(root, day, source="stream")` — compacts every `source=stream` partition of a day.
- Storage-narrowed dtypes (Float32 / nullable UInt8) round-trip through read as the Float64 compute dtype.
- The reader still globs `data*.parquet`, so `data-compacted.parquet` is read with **zero reader change**.

The mechanism is done. It is simply **not invoked anywhere** — `grep` across `ops/`, `scripts/`, and the
cron registry finds no caller (only a doc-string mention). Today's 06-16/17/18 stream partitions still carry
their full 5,656–10,664-file load.

## Proposal (cron hook only — no format change)

Hook the existing compactor into the existing **T+1 post-close lifecycle**, after the day is settled. The
natural seam is `ops/daily_lifecycle.sh` (the 18:30 PT post-close ACQUIRE→SWEEP chain): the just-closed
session's stream partitions are complete, so compacting them is safe and frees the inodes before the next
day's writes. Two equally-safe options:

1. **Append a STAGE 3 to `ops/daily_lifecycle.sh`** — after the sweep, `compact_day(/store, <last session>)`
   for `source=stream`. One subprocess, idempotent, non-destructive to data (union+de-dup, byte-identical
   cells), self-healing on crash. Runs on the day the sweep just validated, so a parity regression would be
   caught on the *un*-compacted files first (compaction never runs ahead of validation).
2. **Standalone cron** `compact_stream` (e.g. `45 18 * * 1-5`, after `daily_lifecycle`), documented in the
   `docs/OPERATIONS.md` registry per the cron-safety checklist (idempotent ✓, RTH-safe ✓ post-close ✓,
   recovery path = re-run ✓, logged ✓).

Recommendation: **option 1** (one chain, one log, compaction strictly *after* that day's parity verdict).

### What this is NOT

- NOT a write-format change to the fc hot path — the live per-minute append stays exactly as is (it is the
  right hot-path shape). Compaction is a T+1 tidy of *settled* partitions only.
- NOT a reader change — `data-compacted.parquet` matches the existing `data*.parquet` glob.
- NOT a fingerprint/schema change — same columns, same dtypes, same cells (de-dup only removes exact
  re-deliveries, which the reader already collapses on union).

### Ask of the Lead

Approve adding the compaction step to the daily lifecycle + its `docs/OPERATIONS.md` registry row. No format
or schema approval is needed (mechanism + reader unchanged); this is purely scheduling an existing,
tested, idempotent, non-destructive tidy job that is currently dormant.

### One-time catch-up

The already-accumulated settled stream days (06-16/17 and earlier) can be folded once by hand —
`python -m quantlib.features.compact /store <YYYY-MM-DD> stream` per settled day — reclaiming ~330k inodes
and ~5× of the 2.74 GB before the cron keeps it tidy going forward. (Leave the *current* still-writing day
to the next post-close run.)
