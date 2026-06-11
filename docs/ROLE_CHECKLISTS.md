# Role checklists — a FLOOR, not a ceiling

Each role has a checklist of EXAMPLE concerns below. Rules:
1. **Floor:** every wake, at least consider these — they guarantee baseline coverage so
   the obvious stuff is never missed.
2. **Ceiling = none:** these are EXAMPLES, not limits. Your real job is to go BEYOND them
   and raise broader concerns toward the goal (see `MISSION.md`). The value is usually in
   what's NOT on the list yet.
3. **Coverage questions (bottom-up blind-spot catcher):** every report ends with
   "**COVERAGE QUESTIONS FOR THE MANAGER: is anyone owning / thinking about X, Y, Z?**" —
   concerns you noticed that may be outside your lane or falling between roles. The Manager
   MUST answer each (assign an owner or confirm coverage) and log it. This is how we catch
   the things that slip between narrow agendas.

---

## QA / Data Integrity — examples
- Calendar/DST/ET correctness across all sources; off-grid timestamps.
- NaN/Inf rates; per-feature warmup/coverage by date (no silent NaN-degrade).
- Backfill↔real-time PARITY — especially TRADE/QUOTE aggregates (the weakest, least-proven).
- Point-in-time universe; survivorship (symbol-list bias on deep history).
- Prediction tradeability (score degeneracy / tie-break baskets).
- Storage/partitioning/compression; retention.
- *Beyond:* any way the data could be quietly lying to us or to the model.

## Modeller — examples
- Is the IC REAL or an artifact? (shuffle canary, calendar leakage, lookahead).
- Loss alignment (rank/lambdarank/vol-scale); horizon (intraday vs OVERNIGHT).
- Feature value/importance; what new signal to add (order flow, momentum, regime).
- Time depth / independent samples; after-COST survival; multiple-testing deflation.
- *Beyond:* is the whole strategy thesis sound, or are we polishing a dead end?

## Production Eng / Architect — examples
- Services up + data fresh; concurrency/perf at the open; recovery.
- Deploy correctness (RUNNING == intended); the stale-code class of bug.
- DB scaling/storage; backfill depth + efficiency (current-month churn).
- Tech debt (`TECH_DEBT.md`); framework choices; scheduled core-rebuilds.
- The data-breadth architecture (sharded universe-wide trade/quote ingestion).
- *Beyond:* will this architecture get us to money or wall us in as we scale?

## Execution / Risk — examples
- Executor correctness vs the `EXECUTION.md` foot-guns (no long+short same name, ETB
  shorts, marketable-limit, wash-trade, sequencing).
- Caps + daily max-loss KILL SWITCH bind from a FRESH broker snapshot; persisted halt flag.
- Reconciliation (DB vs broker); idempotency (intent before submit); staleness + degeneracy
  guards; EOD flatten; truthful P&L.
- Are we safe to flip dry-run → live, and is there signal worth trading yet?
- *Beyond:* what could lose real money or corrupt state that nobody is watching?

## Engineering Manager — examples
- Surface what each agent accomplished; synthesize vs the north star; direct/nudge.
- Scan `RESPONSIBILITY_MAP.md` for ORPHANS; confirm each owner CLOSED THE LOOP.
- ANSWER every agent's "is anyone owning X?" coverage question; assign + log.
- Ensure RUNNING == intended after any change.
- *Beyond:* what is the team NOT doing enough of toward eventually making money?
