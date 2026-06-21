# Pre-flight latency profiler (`profile_sim`)

`quantlib.features.profile_sim` fake-runs the **actual production streaming path** (the real
`StockDataStream` against the protocol-faithful msgpack mock → the same shard workers → the incremental
fast path) and answers the two questions you need **before the open**:

1. **Can we compute the minute's vectors fast enough as bars flow in?** — the end-to-end
   bar-arrival → universe-vector-ready latency (p50/p95/p99) vs the ~100ms budget.
2. **Which feature group is the slowest?** — a per-group `compute_latest` ranking, so a slow group is
   *named*, not hidden inside an aggregate "rest" number.

## Run it

```bash
docker run --rm -v "$PWD":/app -w /app --env-file .env fp-dev \
    python -m quantlib.features.profile_sim <n_symbols> <n_shards> <measure_minutes> [warmup] [window]

# example: a 1000-symbol / 16-shard pre-flight, 20 measured minutes after a 30-minute warmup
docker run --rm -v "$PWD":/app -w /app --env-file .env fp-dev \
    python -m quantlib.features.profile_sim 1000 16 20 30 300
# or: make fp-profile-sim N=1000 SHARDS=16
```

Defaults: `warmup=30`, `window=300` (the production trailing-buffer depth). The tool sets
`MOCK_TRADES_PER_MIN=24` / `MOCK_QUOTES_PER_MIN=72` (a realistic liquid-name tick firehose) so the tick
path (trade_flow / quote_spread / liquidity / tick_runlength) is exercised, not a token 5/min.

## How to read the output

### END-TO-END bar→vector

```
END-TO-END bar-arrival(last bar) -> universe-vector-ready (slowest shard each minute, write excluded):
    p50=   514.2ms  p95=   600.0ms  p99=   603.0ms  max=   603.0ms
    => p99 vs 100ms budget: FAIL (6.0x over)
```

- **Anchor = the minute's LAST bar in hand** (the reader's `dispatch_wall`), the same anchor
  `real_capture`'s `feature_assemble_seconds` uses. It **excludes** Alpaca's per-minute bar-delivery
  spread (a separate, network-bound cost) and the **post-bet parquet write**.
- **Per minute we take the SLOWEST shard's vector-ready wall-clock**, because the universe-wide feature
  vector is not ready for a bet until *every* shard has finished its slice (this is the "per-ticker/shard
  + gather" latency from the operating memory, not a slowest-shard-p99 of an unrelated metric).
- This number is *higher* than the per-shard compute reported by `stream_sim._report` because it includes
  **queue-wait and cross-process contention** — under flood every shard competes for cores at once, which
  is the honest worst case. The sim floods (no inter-minute sleep), so it is a stress ceiling, not the
  real-time arrival cadence.

### PER-GROUP ranking

```
PER-GROUP compute_latest ranking (slowest shard each minute, post-warmup):
    group                              p50       p99       max
    momentum_run                   232.48ms   312.61ms   312.61ms
    residual_analysis               49.85ms    80.07ms    80.07ms
    market_context                  25.06ms    51.88ms    51.88ms
    ...
    TOP-3 slowest groups (p50): momentum_run (232ms), residual_analysis (50ms), market_context (25ms)
```

- Per `(group, minute)` we take the **slowest shard's** time, then the p50/p99/max across the post-warmup
  minutes — consistent with the end-to-end view.
- **Reduction groups** share one batched marshal+kernel, so they have no meaningful *per-group* cost; the
  ranking shows them at the reduction-emit phase time split evenly (context only). The **at-T**
  (non-reduction) groups are the ones whose individual `compute_latest` is timed exactly.
- The slowest at-T groups are the optimization targets. As of this writing the dominator is
  **`momentum_run`**, which uses the base-class `compute_latest` (full `compute()` over the whole buffer,
  then filter to T) — see `docs/LATENCY_PLAN.md` / the findings in the PR that added this tool.

## What to watch at the open

- Live Grafana `feature_assemble_seconds` (last-bar anchor) and `feature_vector_latency_seconds`
  (first-bar anchor, includes Alpaca delivery spread) per shard — the production analogue of the
  end-to-end number here.
- `feature_group_compute_seconds` per group — the live analogue of the per-group ranking. If a group an
  agent just added climbs this chart, this pre-flight tool would have caught it.
- `latency_slow_symbols` (TimescaleDB) — which *tickers* were slowest, when a shard runs hot.

## Relationship to the other profilers

- `quantlib.features.profile [--latest]` — a **single-process, synthetic** per-group table (no sharding,
  no contention, no streaming). Fastest to run; good for "is this one group slow in isolation".
- `quantlib.features.stream_sim` — the **multiprocess streaming** sim with a *phase* decomposition
  (tick-agg / fold / reduction-emit / stateful-emit / gather / rest). Good for "which phase dominates".
- `quantlib.features.profile_sim` (this tool) — the streaming sim with **end-to-end bar→vector latency**
  + **per-group** (not per-phase) attribution. Good for "can we make the open, and which group to fix".

## The e2e latency REGRESSION GATE (`make fp-latency-e2e`)

`profile_sim` above is a **pre-open eyeball** tool you run by hand. The **regression gate** turns the same
measurement into a checked-in pytest so an e2e bar→vector slowdown can't land silently — the e2e companion
to the per-group `docs/latency_budget.yaml` gate.

```bash
make fp-latency-e2e          # un-skips + runs tests/test_fp_latency_e2e.py (heavy; ~10-15s)
```

- **What it does:** drives the *identical* real streaming path as `profile_sim` (via the shared
  `run_profile_sim`), at a **bounded reference scale** (256 syms / 8 shards / 10 measured minutes ≈
  32 syms/shard — amortized like the production 1000/16 layout, NOT the tiny-N coordination-overhead
  regime), and asserts the measured **p50/p99 stay under the ceilings in `docs/latency_e2e_budget.yaml`**.
- **Opt-in / not in the fast suite:** it spins up a multiprocess mock + shard workers, so it is **skipped
  unless `FP_LATENCY_E2E=1`** (the `make` target sets it). It is NOT in `make test-fp`.
- **CEILING, not the 100ms target.** The gate enforces a **generous current-reality ceiling** (seeded
  ~2.4x the measured value, headroom for host-load jitter) so it **PASSES at today's latency** and only
  trips on a real >~1.6x e2e regression. The **<100ms p99 target is aspirational** — stated in the budget
  file and the report banner, NOT enforced here (it would fail today; it is what the latency campaign
  drives toward). Measured at seed (2026-06-21, origin/main, box load ~7.5): **p50 ~131ms / p99 ~175ms**
  at the reference scale (cross-check at the full 1000/16/20: ~255ms / ~324ms — these numbers move with
  host load, hence the headroom).
- **On a breach:** investigate (a slow group, a lost incremental emit, a gather/IPC blowup). Re-run
  `make fp-profile-sim` + `make fp-profile-latest` to attribute the slow stage, fix worktree→PR, and
  re-seed the ceiling ONLY on a deliberate, reviewed change.
