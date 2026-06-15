# Candidate deploy VERIFIED on live data — 2026-06-15 15:53 ET

Branch `deploy/candidate` (= integration/converged tip + DEFAULT_BUFFER_MINUTES 750→300 + feature_scan) is
LIVE on the capture (restarted 15:38 ET) and healthy (fresh < 3 min, 11,336 symbols, NOT stalled).

## Verified on live data (the real "done" bar)
- **breadth fix** — `distinct breadth_up_5m = 1` per minute (was 8 per-shard). ✅
- **points() fix** — features that were 100% DEAD all day are ALIVE once the buffer warmed (≥ window):
  - `efficiency.efficiency_ratio_5m`: 3359/4260 non-null
  - `momentum_consistency.consistent_direction_5m`: 3418/4260 non-null
  - longer horizons (10m, `ret_accel`) still warming — need deeper buffer; scanner tracks the fill.
- std=0 guards, buffer-300 perf, continuous `feature_scan` (cron /6min): deployed.

## Process corrections (logged to memory: feedback-verification-culture COLD-BUFFER RULE)
Three premature failure-conclusions today, all the same cause: reading a freshly-restarted (cold-buffer)
system. "Stall" was normal ~4-6min cold-start; "points fix broken" was a 6-min-deep buffer before the 5m
lags could populate. RULE: establish the baseline recovery time; wait for buffer depth ≥ feature window
before declaring dead; trust the full-day-aware scanner trend.

## Open
- Reconcile branches (deploy/candidate is the live line; clean the embedded-worktree commit; rebase loops).
- Re-enable the two autonomous loops with the new discipline (done = verified on live data).
- Scrub "60s budget" framing from converged-line docs (budget = ~100ms, not the minute cadence).
- Longer-horizon points + other systemic items (sector_map, ETF universe, market_beta) continue.
