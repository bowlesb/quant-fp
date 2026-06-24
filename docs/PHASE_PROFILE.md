# Incremental phase profiler (`phase_profile`)

`quantlib.features.profile` / `latency_expectations` give **one** number per group: the wall time of
`IncrementalEngine.step` for an `incremental_safe` reduction group. That number is a black box — it does
**not** say whether the cost is the *arithmetic* (the running-sum fold) or the *framework* (per-minute
polars expression evaluation over the trailing buffer). `quantlib.features.phase_profile` opens the box.

## What it measures

For every armed `incremental_safe` `ReductionGroup`, on the same seeded engine the live capture runs, it
breaks `step` into its four phases:

| phase | what it is | typical share |
|-------|-----------|---------------|
| `matrix_at` | `_matrix_at`: per-minute polars derive (slice/sort/group_by/tail over the whole buffer + the short-lag exprs) marshalled to the numpy value row | ~2.5–4 ms |
| `fold` | `state.update`: the Neumaier running-sum update — the **actual O(1) arithmetic** (+ `_roll_time_origin` + `trim`) | **~0.05–0.16 ms** |
| `resolve_points` | `resolve_points`: the lag/point exprs over the **whole** buffer (full sort + select + filter) | ~2–4 ms |
| `assemble` | `assemble_from_long`: numpy sums → long polars frame + the **per-group** pivot / rename / join / `assemble()` exprs | ~0.7–2.5 ms |

The four phases sum to ~the `step` total. It also times the gated-off emit twins (`step_numpy`,
`step_rust_unified`) for the same group so the headroom of the already-written faster assemble paths is
visible next to the live default.

## The headline finding

At the reference shard (312 tickers × 245 m), across the 18 live incremental groups:

```
arithmetic (fold)  =   0.9 ms  ( 0.7%)
polars overhead    = 108.0 ms  (85.6%)   (matrix_at + resolve_points + assemble)
```

The "incremental O(1) fold" is genuinely O(1) and costs **microseconds**. ~86 % of every group's time is
polars expression evaluation over the buffer, repeated every minute. Live the 18 groups share **one**
`minute_agg` engine (≈ 41 ms, not the 125 ms standalone sum), within which `assemble_from_long`'s per-group
pivot loop is the single biggest line (~53 %); `matrix_at` (~5.7 ms shared) and `resolve_points` (~4 ms
shared) are the next two and are **not** touched by the emit twins (a deeper structural prize).

## Run it (load-gated, never `-n auto`)

```bash
docker run --rm --cpus 6 -e POLARS_MAX_THREADS=4 -e ALPACA_KEY_ID=mock -e ALPACA_SECRET_KEY=mock \
    -v "$PWD":/app -w /app fp-dev \
    python -m quantlib.features.phase_profile [n_tickers] [window_min] [reps]
```

Defaults (312 × 245 m) match the latency-expectations reference shard, so the per-group `step_ms` here line
up with the dashboard's incremental rows. No secrets / DB needed — it runs on the synthetic
`build_frames` fixtures the rest of the profiler suite uses.
