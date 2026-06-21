# Harness dogfood — running friction log (raw, append-only as hit)

Chronological, candid. Synthesized + prioritized in README.md. Stamped against
`quantlib/harness/` @ origin/main 6671f6b.

## Cold-start (the very first thing a new user does: copy the docs example)

1. **The documented "just run it" example fails on a fresh checkout.** Both
   `docs/STRATEGY_HARNESS.md` and the module docstring show
   `run_strategy(HarnessConfig(daily_cache="experiments/data/battery_daily_cache.parquet"))`
   and `python -m quantlib.harness --daily-cache experiments/data/battery_daily_cache.parquet ...`.
   That parquet does **not exist in the main tree** (it is gitignored / per-worktree — it
   only exists in some `.claude/worktrees/*`). A new user copying the example verbatim with
   no `/store` mounted hits the cache-miss path.

2. **The cache-miss error is opaque and non-actionable.** With no cache and no store, the
   first run dies deep in polars with `ValueError: cannot concat empty list`
   (`build_daily_table` -> `pl.concat(rows)` over an empty `rows`). Nothing says "no raw bars
   found for date range X under STORE=/store — did you mount the store / is the range right?".
   A user has no idea the real cause is an empty raw-bar glob.

3. **Nothing tells you the store must be mounted, OR how.** The store is a docker *named
   volume* `fp_store_real`, not a host path. The `make` `FP_RUN` macro mounts only `$PWD:/app`,
   never `/store`. None of the harness docs mention `-v fp_store_real:/store:ro`. I only found it
   by grepping `docker-compose.yml`. A host `-v /store:/store` (the obvious guess) silently
   mounts an EMPTY dir -> the opaque error in (2).

4. **Cold first run is ~2m44s with ZERO progress output.** The uncached first call globs the
   raw-bar store (7703 symbols x ~130 dates) to build the daily cache. The docs advertise
   "1-6s" / "Runtime 1-6s" — that's the WARM number; they never state the first run is minutes
   and silent. The user sees a hung-looking process with no spinner / no "reducing date N/130".

## Output / artifacts

5. **`--out` writes INSIDE the `--rm` container and is lost.** `--out /tmp/harness_ridge` wrote
   report.md/csv/json to the container fs; with `--rm` and no mount of that path, the artifacts
   vanish on exit — only stdout survives. The daily *cache* persisted only because it happens to
   live under the bind-mounted `/app`. A user must know to point `--out` under `/app` (and the
   docs example uses `/tmp/harness_demo`, which is exactly the path that gets eaten).

## Single-feature / "test ONE idea" (the dogfood task's framing)

6. **The DAILY path has NO knob to test a single feature.** `--intraday-groups` only applies to
   `cadence=intraday`. The daily panel always carries all 13 hard-coded `DAILY_FEATURE_COLS` and
   every model uses all of them. There is no `--daily-features a,b` / `feature_subset` on the
   config. So "evaluate this ONE trusted daily feature as a strategy" is not expressible on the
   daily path without editing source. (Workaround: use the intraday path with a 1-feature group,
   which is what I did — but that forces the store-join path + the limited backfill window.)

## Doc consistency

7. **Cache-path inconsistency across docs.** `docs/STRATEGY_HARNESS.md` uses the *relative*
   `experiments/data/battery_daily_cache.parquet`; `quantlib/battery/README.md` uses the
   *absolute container* `/app/experiments/data/battery_daily_cache.parquet`. Minor, but a user
   copy-pasting between them gets different behavior depending on cwd.
