# Feature-store format & read/write stewardship review

A Warehouse stewardship review of the feature-store on-disk format and every read/write path that
touches it. Scope: confirm the write paths are parity-by-construction consistent, confirm the read
paths are robust, and answer the forward-looking question the Latency centering work raises — *are the
store reads robust to additive column growth?* This is a **review note (docs only)** — it proposes,
it does not change. Verdict up front, then the evidence.

## Verdict

**Clean bill, with one defense-in-depth hardening recommendation (no code changed here).**

- The two write paths (real-time capture and backfill) call the **identical** `store.write_group` and
  emit the **identical** column set (enforced structurally), so the store is parity-by-construction by
  design — not by convention.
- Every read path is robust to additive feature growth, because additive growth **within a partition
  tree is structurally impossible**: the engine refuses to persist any undeclared column, and a new
  declared feature changes the fingerprint and the `v=<version>` partition key, isolating the new
  schema in a fresh tree. The Latency centering work (intermediate power-sum columns) cannot reach
  disk and cannot perturb any reader.
- **One recommendation (propose-only, Lead's call):** the multi-file `pl.scan_parquet` reads pin a
  single `v=<version>`, which is what keeps them safe today. If the version-bump discipline is ever
  bypassed (a feature added under an unchanged version), polars 1.41.2 raises `SchemaError` on the
  heterogeneous-schema scan rather than degrading gracefully. A one-line `extra_columns="ignore"` on
  those scans would make the read paths self-healing against that operator error. This is belt-and-
  suspenders, not a live defect — flagged, not fixed.

## The on-disk format

Layout (`quantlib/features/store.py` docstring, FEATURE_PLATFORM.md §3.3.1):

```
<root>/group=<g>/v=<version>/source=<stream|sim|backfill>/date=<YYYY-MM-DD>/data*.parquet
```

- **Key columns** are `("symbol", "minute")` — the single `KEY_COLUMNS` constant in
  `quantlib/features/base.py:17`, imported by every writer and reader. There is one definition of the
  key, used everywhere.
- **Feature columns** are stored **narrowed** (`storage_dtype`, `base.py:76`): Float32 for the
  real-valued bulk, nullable UInt8 for 0/1 flags, smallest-int for the four integer calendar features.
  Compute is always Float64; reads widen back to Float64 (`store.py:191-193`) so every consumer sees a
  uniform dtype and the narrowing is a pure disk concern. Parity compares stored-vs-stored, so both
  paths round identically.
- **`source`** physically separates provisional-live (`stream`), simulated-live (`sim`), and settled-
  truth (`backfill`). A `_store_mode` marker (`store.py:69-85`) refuses to mix real and mock data in
  one root.
- **File granularity:** the stream writes one tiny per-minute file per shard (`data-<shard>-<epoch>.parquet`,
  O(1) per tick); backfill writes one settled `data.parquet` (or `data-<chunk>.parquet` per chunked
  sweep). Reads glob `data*.parquet`, so every per-minute / per-chunk file reads back as the union.
  (Settled stream partitions are folded to a single file by the scheduled compaction — see
  `docs/STREAM_COMPACTION.md`.)

## Write path — capture and backfill are one path

Both live capture and backfill funnel through the same two functions, which is the parity substrate:

- **Real-time capture** (`quantlib/features/capture.py:410-420`) computes a minute, then calls
  `store.write_group(..., source=source_for_mode(mode), minute=latest)` — append mode, one file per
  (shard, minute).
- **Backfill** (`quantlib/features/materialize.py:55,184`) computes a day from the raw tape, then calls
  `store.write_group(..., source="backfill")` — whole-partition mode.

Both compute via `engine.run_group` (`quantlib/features/engine.py:27`), which **always** runs
`_validate_schema` (even in production, where range validation is off — `engine.py:28-33,48-59`). That
guard asserts the written frame's columns are **exactly** `KEY_COLUMNS ∪ {declared feature names}` and
**raises** on any undeclared column. So:

> Nothing reaches disk except declared features. The two paths cannot drift in column set, dtype, or
> key — they are the same code computing the same contract.

`write_group` itself is atomic (write-temp-then-`os.replace` within the partition dir, `store.py:122-125`)
and idempotent (a re-delivered minute overwrites its own file), and shard-disjoint (N concurrent capture
workers each write their own file, never clobbering). These are the Monday collect+save concurrency and
crash-safety properties; they hold for both sources.

## Read paths — inventory and robustness

| Read path | Where | Selects | Robustness |
|---|---|---|---|
| Training / parity read | `store.get_features` → `_scan_source` (`store.py:174-197`) | `KEY_COLUMNS + requested feats`, single `v=` | Column-pruned scan; `require_settled=True` raises on stream-only dates so a model never trains on provisional data |
| Coverage grid (live dashboard) | `store_grid.py` + `feature_grid._read_symbols` (`feature_grid.py:182-193`) | **`symbol` column only**, single `v=`, bounded file sample | Never touches feature columns → immune to feature schema changes entirely |
| Stream-universe roll | `store.stream_symbols_on` (`store.py:251-267`) | `symbol` column only, globs `v=*`, **per-file** read | Per-file single-column read deliberately sidesteps the mixed-schema reject (documented in-place, `store.py:257-260`) |
| Coverage / depth aggregation | `feature_grid.gather_group_store_info` (`feature_grid.py:208-221`) | `symbol` column only, single `v=` | Symbol-only; same immunity as the grid |
| Health / dead-feature scan | `quantlib/ops/feature_scan.py:56` | all columns, **one day** partition, `vertical_relaxed` concat | Single-day scope → no cross-time schema boundary; relaxed concat tolerates a within-day superset |
| Live-monitor freshness | `quantlib/ops/healthcheck.py:183` | all columns, one stream partition | Already passes `missing_columns="insert"` |
| Compaction | `quantlib/features/compact.py:44` | all columns, one partition | Already passes `missing_columns="insert"`; reader-transparent (`data*.parquet` glob) |

The two coverage/grid surfaces — the ones a schema change is most likely to be blamed for — read **only
the `symbol` key column**, so a feature added/removed/renamed cannot affect them.

## Forward-looking flag: additive column growth (the Latency centering work)

The reduction-stability centering (PR #304 + its continuation) will "wire the additive centered
power-sum columns (Σ(v−a), Σ(v−a)²) into `build_plan`." The stewardship question: **do the store read
paths break when a group's output grows a column?**

**They are robust, because additive growth never reaches a shared partition tree.** Two structural
facts, verified:

1. **Intermediate columns never persist.** Those centered power-sum columns are `build_plan`
   intermediates. `_validate_schema` (`engine.py:48-59`) refuses to write any column that is not a
   `declare()`d feature, so an un-declared intermediate cannot land on disk. PR #304's own commit says
   the anchor layer is "UNWIRED (additive, no group centers yet) → byte-identical, fp 0x873f
   unchanged" — consistent with this guard.

2. **A new *declared* feature forces partition isolation.** If the centering work promotes a column to
   a real declared feature, that changes the feature-set fingerprint — a blake2b over the ordered
   `group:feature:version` lines (`quantlib/bus/schema.py:47-48`) — and, per the project's
   version-bump discipline, the group `version`. The partition path keys on `v=<version>`
   (`store.py:50`), so the new schema is written under a **fresh `v=` tree**. The old tree keeps its
   old schema; the new tree holds the new one. No partition ever contains mixed-schema files, because
   every file in a `group=/v=/source=/date=` partition was written by the same registry version's
   identical `declare()`.

So under the **intended** discipline (declare ⇒ fingerprint bump ⇒ new `v=`), additive growth is a
no-op for readers: each reader pins one version, sees one schema.

### The one fragility worth flagging (defense-in-depth, propose-only)

The single-version pin is what makes the multi-file scans safe. To quantify the failure mode if that
discipline is ever bypassed — a column added to a group **without** a version bump, so the same `v=`
tree holds old (column-absent) and new (column-present) files — I measured polars 1.41.2's behavior on
a heterogeneous-schema multi-file scan (the `_scan_source` shape):

- default `scan_parquet([...]).select([...])` → **`SchemaError: extra column in file outside of
  expected schema`** (hard fail), regardless of whether the missing/extra column is the one selected.
- `missing_columns="insert"` does **not** rescue this — it handles a *missing* column, not an *extra*
  one; it still raised `SchemaError`.
- `extra_columns="ignore"` is the knob that tolerates it (and `vertical_relaxed` concat tolerates the
  read-all case).

This is **not a live defect** — today every multi-file scan pins one version, so no partition is
heterogeneous. But the failure is a hard crash, not graceful degradation, and the protection is purely
operational (remember to bump the version). **Recommendation (Lead's call, no code changed here):** add
`extra_columns="ignore"` to the version-pinned multi-file scans (`store._scan_source`, and the same
pattern in any future store reader) as belt-and-suspenders, so an un-bumped additive column degrades to
"new column ignored by old-schema reads" instead of crashing the parity sweep / training export. The
`store.stream_symbols_on` cross-version glob is *already* safe by a different mechanism (per-file
single-column reads), and the coverage grid is safe because it reads only `symbol`.

## What I did not find

No drift between the capture and backfill column sets (the schema guard makes drift impossible). No
under-documented partition layout (the `store.py` module docstring and FEATURE_PLATFORM.md §3.3.1 both
state it). No read path that materializes feature columns across a version boundary. The store-format
substrate is consistent and well-documented; the only forward-looking item is the optional
`extra_columns="ignore"` hardening above.
