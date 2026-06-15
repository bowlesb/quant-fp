# Verification Culture — aggressively try to break our own work

The disease this fixes (proven 2026-06-15): ~15 fixes were marked "done" because a branch merged and a
1,250-ticker unit test passed. None ran in production. When finally deployed they broke live collection at
11k. **Code that isn't running and being attacked is not progress — it's a liability that looks like progress.**

## Definition of DONE (non-negotiable)
A change is DONE only when it is: **deployed → verified at LIVE SCALE (the real ~11k universe) → the target
metric/parity actually moved → and a standing check defends it going forward.** "Merged" / "unit test green"
/ "validated at 1,250 tickers" are checkpoints, not done. If it isn't running and observed, it isn't done.

## The three layers of defense (run continuously, forever)
1. **Layer 1 — continuous automated scan** (`quantlib/ops/feature_scan.py`, cron every 6 min): per-feature
   NaN%/dead/const/inf across EVERY group, full-day-aware (structural-dead vs warmup). LLM-free, cheap,
   repetitive. The change→observe instrument: re-run it after any change and SEE the effect immediately.
   Plus `ops/healthcheck.sh` (every 5 min) for liveness/coverage/latency/parity.
2. **Layer 2 — per-group defender subagents** (the audit loop): each OWNS a group, looks at the ACTUAL
   numbers, questions whether they are correct, tries creatively to break them — and **re-checks on rotation
   FOREVER** (never "all OK, done"). Re-audit previously-green groups regularly; a quiet group is a group
   nobody has attacked recently.
3. **Layer 3 — change→observe discipline**, especially DURING MARKET HOURS (the only time we can change a
   thing and watch the live data improve in minutes): make ONE small isolated change → canary → observe the
   metric move on the scanner → keep or roll back. Small and reversible; never a 15-change big-bang.

## Operating principles
- **Re-do the same tests over and over.** Repetition is the point, not waste. A check that passed yesterday
  must pass again today, run again, unprompted. Regressions hide in things nobody re-checks.
- **Look at the numbers, don't trust the green checkmark.** Open the data. Is this value plausible? Why is
  this feature 40% NaN? What's behind a column of no values — warmup, or a dead code path?
- **Be creative and diverse.** Distribution shape, sign vs the tape, cross-symbol sanity, range bounds,
  warmup curves, parity live-vs-backfill, duplicates, look-ahead. One angle never catches everything.
- **A bug found is the goal, not a failure.** Breaking our own thing first is cheaper than the market doing it.
- **Market hours are precious.** When the feed is live, invest aggressively in change→observe; that feedback
  loop does not exist after close.

## Canary deploy (how to change→observe without risking production)
Prefer a small-universe / mock-fed canary, or a single perf-neutral isolated change with an auto-rollback
freshness guard. The 2026-06-15 outage was a 15-change big-bang restart with no canary and no scale test —
exactly what this forbids. Note: Alpaca allows ONE websocket per account, so a second LIVE-feed canary on the
prod account is not possible — use the mock feed or a post-close window for scale validation.
