# Tech-Debt Ledger — owned by the Architect (in Production Eng)

A self-evolving system accretes complexity; this ledger keeps it deliberate. The
Architect triages this every wake, and SCHEDULES periodic "rebuild core things"
maintenance instead of letting debt compound silently. Severity: P1 bites soon, P3 later.

| sev | item | why it's debt | rebuild/repay plan |
|-----|------|---------------|--------------------|
| P1 | experimenter ran STALE code → wrong results | no "running==intended" gate before trusting output | rebuild+restart+verify after edits (Manager duty added); consider a code-version stamp in experiment records |
| P1 | rebuild = ON CONFLICT DO NOTHING (can't overwrite) | recompute can't replace stale rows (today-panel UTC bug) | switch panel rebuild to DELETE-then-insert |
| P2 | build_feature_store ~4k sequential round-trips/cadence + per-symbol daily-close query | N+1; fine at 30m, won't scale to tighter cadence/universe | batch bar/daily-close loads (ANY(array)); hoist shared queries |
| P2 | trades/quotes only for 10 symbols | blocks universe-wide order-flow features (modeling roadmap) | the Architect's sharded ingestion-tier decision (see JOURNAL) |
| P2 | ETF exclusion is a name-regex stopgap | fragile; may miss/over-match | proper ETF reference list |
| P3 | feature_vectors/labels/predictions uncompressed | storage growth at scale | enable compression once panel-rebuild churn settles |
| P3 | experimenter writes host files as root | permission paper-cuts | add user:uid to the service |

## Scheduled core-rebuilds (maintenance windows)
- (none scheduled yet) — Architect proposes one when debt in an area crosses a threshold.
