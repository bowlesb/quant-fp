# Scalability Design — timing + process-parallelism as first-class citizens

Goal (Ben, 2026-06-13): the feature platform must support **an order of magnitude more features AND
tickers** than today without ever worrying about scalability again — *as long as every feature is
timed and fast*. Two things are mandatory and first-class, learned from the prior Edgar system:
1. **Every feature's compute latency is measured and queryable** ("time the hell out of every one").
2. **Work is split across all processes**, not just polars threads.

No pruning. We redesign the compute substrate for headroom, then features are cheap to add forever.

## The numbers (mem_bench, 2026-06-13, 502 minute-path feats / 26 groups, 32-thread box)
| config | time | RSS |
|---|---|---|
| 10k tickers × 120m buffer, single process | 47.5 s | 10 GB |
| 10k tickers × **300m** buffer (correctness: 240m windows), single process | **140 s** | 25 GB |
| 2.5k × 120m | 9.1 s | 2.7 GB |

Single-process vectorized polars is **2.3× over the 60s minute boundary** at the correctness buffer.
That is the problem to solve. The per-group profiler (`python -m quantlib.features.profile`) shows the
cost is concentrated: the **OLS-kernel groups** (`price_volume`, `trend_quality`, `return_dynamics`,
`distribution`, `market_beta`) are 10-30× the µs/feature of simple rolling groups — so the fix is
targeted, not a prune.

## Why my first instinct ("sharding won't help") was wrong
The Edgar system computed features **per symbol** (NumPy reductions → one scalar per feature) and ran
**12 processes** partitioned by `hash(symbol) % 12` — near-linear speedup, ~28 ms for 850 features
*per symbol*. My platform instead vectorizes across **all** symbols in one polars call. That call:
- already multithreads, so I assumed process-sharding adds nothing; BUT
- it **computes every buffer row then discards 299/300** (live needs only minute T), and
- `.over("symbol")` rolling has sort/group overhead that scales sub-linearly with threads.

So there is large headroom from (a) computing only what live needs and (b) process-parallelism.

**Clean A/B confirms it (10k × 502 feats, 120m buffer, 32-thread box, steady-state per minute):**
| config | per-minute compute |
|---|---|
| 1 process × 32 threads | 47.5 s |
| 16 procs × 625 names × 2 threads | ~16 s |
| 32 procs × 312 names × 1 thread | **~14 s** |

**Symbol-sharded multiprocessing is ~3.4× faster than single-process multithreading** — polars
`.over("symbol")` rolling scales sub-linearly with threads, and independent per-shard processes
recover near-linear parallelism (Edgar's model, vindicated). Workers are PERSISTENT, so warmup is paid
once at startup, not per minute. Projected to the 300m correctness buffer: 140s → **~41s, under the
60s boundary** — and that is BEFORE the latest-minute optimization, which removes the ~300× whole-
buffer waste on top.

## Design

### 1. Per-feature timing — FIRST CLASS (foundation, partly built)
- **Offline profiler (built):** `quantlib/features/profile.py` → per-group latency table sorted
  slowest-first, µs/feature, at any ticker scale. Run on EVERY feature batch; a new group that is
  slow is caught immediately.
- **Live timing (to build):** wrap each group's compute in the live path with a perf-counter, emit
  per-group `group_compute_ms` (histogram, like Edgar's Prometheus `FEATURE_GROUP_DURATION`) and a
  per-vector `capture_latency_ms` column in the store — so production latency per group is queryable
  on the dashboard, not just in a bench.
- **Latency API (to build):** `/api/features/latency` returns the per-group timing table + p50/p99
  from live histograms. "APIs where we can look closely at latency for every feature."
- **CI gate (to build):** a test that fails if any group exceeds a per-feature µs budget at a
  reference scale — enforces "timed and fast" as a merge condition.

### 2. Process-parallelism — FIRST CLASS (the work-splitter)
Partition by symbol across a persistent process pool (Edgar's model, adapted to our vectorized
substrate): N worker processes, each owns `hash(symbol) % N` and runs the **identical** vectorized
group code on **its shard's buffer** (single code path → parity preserved; a symbol's per-symbol
features never depend on another symbol). Sizing: N ≈ cores, with `POLARS_MAX_THREADS` set so
N × threads ≈ cores (no oversubscription). The ingestor already shards ticks this way; the feature
computer mirrors it.

**Cross-sectional features need a gather phase.** `cross_sectional_rank` (universe percentile) and
the market/sector broadcasts depend on >1 symbol, so they break naive symbol-sharding. Two-stage:
1. **Map:** each shard computes all per-symbol features for its symbols → writes its slice.
2. **Reduce:** a small cross-sectional stage gathers the needed columns (returns/volume for ranks;
   SPY/QQQ are tiny and broadcast to all shards cheaply) and computes ranks over the full minute.
This keeps the expensive 99% embarrassingly parallel and isolates the cheap cross-sectional 1%.

### 3. Compute only what live needs — the latest-minute window
Live needs minute T's value, not the whole buffer. Two complementary moves, both parity-preserving:
- **Right-size the buffer:** the live buffer must be exactly `max_window + max_lag` (≈ 241m today),
  not an arbitrary 300m — already enforced by the buffer invariant, but it means live never pays for
  history beyond the longest window.
- **Latest-minute emission (bigger win, needs a parity test):** reformulate the live compute so a
  windowed feature at T is a single trailing-window aggregate (`filter(minute > T-w).group_by(symbol)
  .agg(...)` → one row/symbol) instead of `rolling_*_by` over all rows. ~300× less output + most of
  the compute. This is a SECOND formulation, so it ships ONLY behind a standing parity test:
  `latest_form(buffer) == rolling_form(buffer).filter(minute == T)` for every feature. Backfill keeps
  the rolling form (whole day at once is efficient there). Same window spec drives both, so the test
  guards a mechanical transform, not hand-rewrites.

### 4. Materialize-once optimization (DONE on the 4 hot groups — MEASURED, byte-identical)
The hot groups dominated because polars eager does NOT common-subexpression-eliminate rolling work:
each output re-derived its rolling sums. Fix = materialize the shared rolling sums ONCE as temp
columns then derive outputs cheaply (`with_ols_columns` for OLS groups; inline for power/volume sums).
Measured @ 2000 tickers, all byte-identical (every correctness + parity test still passes):
- trend_quality **1984 → 213 ms (9.3×)**
- market_beta **1770 → 172 ms (10×)**
- distribution **1824 → 106 ms (7.5×)**
- price_volume **3067 → 488 ms (6.3×)**
- **Profiler total 13.3s → 3.6s at 2k tickers (3.7×), pure compute, zero parity change.**

Remaining: `return_dynamics` is now the slowest (644 ms) but it is `lagged()`-join-bound (13 self-joins),
not kernel-bound — a separate fix (compute all close-lags in one pass). And groups each `.sort` the
frame; sort once upstream and pass it pre-sorted.

### 5. DB write concurrency (real-time, parallel workers)
Each shard writes only its own symbols, so writes are naturally partition-disjoint (no row
contention). Per-worker connection (Edgar used NullPool — fresh connection per async worker), batched
**COPY** into the Timescale hypertable (segmented by symbol), feature vector stored as a compressed
fixed-width array (Edgar: zlib'd float32 by feature index) to cut WAL volume. Idempotent on
`(symbol, minute, source)` so a re-delivered minute self-corrects. The store's Parquet path is
already partition-disjoint by `group/source/date`.

### 6. Substrate / faster language — the decision criteria
A faster language (Rust/Polars-plugin/numba) is plausibly unavoidable eventually, but we evaluate it
ONLY after the above are in place and measured, because they may already give 10× and Rust is a big
cost. Pull the trigger when: (a) per-feature timing + sharding + latest-minute + kernel-reuse are
shipped, (b) the latency API shows specific groups still over budget at target scale, and (c) those
groups are algebraically irreducible in polars. Then port the hottest groups as a Polars plugin
(Rust) behind the SAME FeatureGroup interface — incremental, not a rewrite — with the parity test
unchanged.

## 10× headroom analysis
Target: 5,000 features × 100,000 tickers per minute. Levers compound:
- Latest-minute emission: ~300× less per-minute work than whole-buffer (the dominant win).
- Process-parallelism: ~cores× (32 now; a bigger box or a second node scales out).
- Kernel + sort reuse: 2-5× on the hot groups.
- Right-sized buffer: removes the 300m→241m overpay.
With latest-minute + sharding, today's 140s/10k-feat-minute target becomes seconds, leaving headroom
for 10× features and 10× tickers before any language change. Every new feature passes through the
profiler + CI latency gate, so the catalog stays fast by construction.

## Build order
1. Live per-group timing + latency API + CI latency gate (make "timed" enforceable).
2. Kernel/sort reuse on the 5 hot groups (cheap, byte-identical, immediate).
3. Symbol-sharded process pool for the live compute + cross-sectional gather phase.
4. Latest-minute emission behind its parity test.
5. Re-measure at 10k (and project 100k); only then weigh a Rust plugin for any remaining hot group.
