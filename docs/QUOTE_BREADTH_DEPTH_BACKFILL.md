# Quote breadth-at-depth backfill — scope, pilot, full plan

**Goal.** Extend the broad ~3,950-symbol quote breadth BACKWARD in time. The deep quote tape filled
depth-first over a ~530-symbol liquid HEAD (which reaches into 2024-12) and only WIDENED to the broad
universe on **2026-03-18** (the breadth onset). So the broad breadth is a recent *shelf*: every non-head
name has quotes from 2026-03-18 forward but NONE before it. This fills the non-head names' missing earlier
dates so the deep quote panel has full breadth at depth.

**Why (non-blocking).** Deep-history foundation for cheap feature invention on the broad universe without
re-download (manifest = no dup), and cost-model regime robustness across more history. NOT a fresh alpha
edge — the quote-alpha G0a screen nulled. Pilot + plan + recommend; the full multi-hour fill is
Lead-budgeted.

## Verified scope (from the raw quotes manifest, 2026-06-21)

- **Breadth onset = 2026-03-18** (3,945 syms), NOT 2026-03-31. Before it, a ~527-530-sym head-only set
  (reaching back through 2025 into 2024-12). Correction to the original task premise.
- **Broad universe** (real tape on the settled ref day 2026-03-23, ex-SPY/QQQ): **3,947 names.**
- **Already reach ≤ 2026-01-02:** 605 (the head + names with earlier listings). **Breadth-depth targets:
  3,342** non-head names (computed live by `quantlib.data.quote_breadth_depth_gap`).
- **Q1 window** (2026-01-02 … 2026-03-17): **51 trading days.**
- **Wider window** (2025-10-01 … 2026-03-17): **115 trading days.**
- Per-partition quote size for non-head names: mean **141.8 KB** (median 69 KB), ~30k rows/day — the
  non-head tape is thinner than the head's (foreign large-caps like HSBC/DB/SAN are the heavy exceptions).

## Pilot (run + verified, 2026-06-21)

20 non-head names (20 heaviest + lightest, to bracket the size range) × 5 Q1 dates (2026-03-09 … 03-13),
quotes-only, guard-named `quant-backfill`, `--processes 1 --quotes-chunk-days 1`, cpus4/mem12g.

- **Result:** 100 partitions, **0.077 GB**, **~88s** fetch wall, exit 0.
- **Verified:** 95/100 (symbol,date) landed with a real tape (rows>0); 18.4M rows. 19/20 names full 5/5.
  The 1 zero-tape name (MLAA) is a late-listing SPAC with genuinely no early-March SIP quotes — recorded
  rows=0 (correct manifest behavior, never re-fetched). Thin microcaps (SUGP 654 rows, FAC 621) landed real.
- **Measured rate:** **~1.14 partitions/sec** at `--processes 1 --threads-per-process 8`, single shard.
- Container `docker rm`'d; manifest reconciled (append-only parts, already queryable).

## Full-plan estimates (measured rate + non-head mean size)

| Window | Targets × dates | Partitions | GB (non-head mean .. heavy upper) | Wall @ P1×T8 single shard | Wall @ 4 disjoint shards |
|---|---|---|---|---|---|
| **Q1** (≥2026-01-01) | 3,342 × 51 | ~170k | **~25 GB** .. 134 GB | ~42 h | **~11 h** |
| **Wider** (≥2025-10-01) | 3,342 × 115 | ~384k | **~56 GB** .. 303 GB | ~96 h | **~24 h** |

- GB lands near the non-head-mean column (~25/56 GB); the heavy upper bound assumes every name is a foreign
  large-cap, which is false. Disk is a non-issue: **1.9 TB free** on the NVMe (`/dev/nvme0n1p2`, 45% used).
- **Wall-clock is the binding cost.** A single P1×T8 shard is ~42 h for Q1. Mitigation = run several
  DISJOINT-symbol-shard containers in parallel (`CONTAINER=quant-backfill-q1-shardN`, each a slice of the
  3,342 names) → ~11 h for Q1 on 4 shards. The Lead budgets the shard count vs live-capture headroom (each
  shard is guard-named so `live_monitor` pauses them, never fc, under host pressure).

## How to run

```bash
# Compute + preview the target set + docker argv (launches nothing):
ops/quote_breadth_depth_fill.sh --dry-run

# Launch the Q1 fill (default window 2026-01-02 .. 2026-03-17), detached, guard-named quant-backfill:
ops/quote_breadth_depth_fill.sh

# Wider window:
START=2025-10-01 END=2026-03-17 ops/quote_breadth_depth_fill.sh

# Parallel shards (Lead-budgeted) — split the symbol set across N containers, e.g. by passing a slice via
# SYMBOLS env or distinct date sub-windows; each guard-named quant-backfill-<shard> and one-at-a-time-safe.
```

`quantlib.data.quote_breadth_depth_gap` computes the target set deterministically from the on-disk quotes
manifest (broad universe on the ref date minus names already reaching back), so the list self-shrinks as
partitions land — re-running only fetches what is still missing. The fetch is `raw_backfill` WINDOW mode,
idempotent per (symbol,date), `--top-trades 0` (quotes only), budget-capped.

## Recommendation

Start with the **Q1 fill (~25 GB, ~11 h on 4 shards)** — it closes the breadth gap to the natural Q1
boundary and is the cheaper proof. Extend to 2025-10-01 only if a downstream cost-model/feature study wants
the deeper window (it is the same driver with `START=2025-10-01`). The full launch is **Lead-budgeted**
(shard count + timing vs Monday capture headroom); the pilot proves the path is clean and the rate/size are
known. Disk poses no constraint.
