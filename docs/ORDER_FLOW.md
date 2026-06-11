# Order-Flow Data — the path to edge (Architect workstream)

Price-only cross-sectional features have NO tradeable edge (proven: 30m real-but-uneconomic;
overnight was survivorship — see EXPERIMENTS.md). The most plausible remaining edge source at
our latency is **universe-wide order-flow / microstructure**. We stream trades/quotes for only
10 symbols today. This is the build.

## Measured volume + the core design constraint (2026-06-11)
- `trades_raw`: ~91k rows/symbol/hour (10 syms = 915k/hr). Universe (~1000) ≈ **91M rows/hour
  ≈ 700M/trading-day** of RAW ticks. **Do NOT store raw ticks universe-wide.**
- `trade_agg_1m`/`quote_agg_1m`: ~59 rows/symbol/hour (1/min). Universe ≈ **~59k/hour = trivial.**
- Throughput: ~25k ticks/sec universe-wide -> too much for one Python process.

**Design:** N SHARD processes, each subscribes to a disjoint symbol subset's trades+quotes,
aggregates IN-MEMORY via the SHARED `quantlib.aggregates` (the parity guarantee), and persists
ONLY the per-minute aggregates. Raw ticks: not stored universe-wide (optionally a short-retention
sample for debugging). Mind the single-Alpaca-data-socket constraint (may need multiplexed
connections / entitlement check before N sockets).

## The hard part — TRADE PARITY at scale (QA invariant I2b)
Live tick-aggregation vs REST backfill-aggregation must MATCH on a settled day (the weakest,
least-proven parity case — bars are ~99.4%, trade-aggs only ~95% on a sliver). Threats: dropped
live ticks vs complete REST, tick-rule order/state, trade-condition filtering, minute-boundary
init. GATE: settled-day trade-agg parity before any order-flow feature enters a trusted model.

## Incremental rollout (validate throughput + parity at each step; do NOT big-bang)
1. 10 -> ~50 liquid symbols (TRADE_QUOTE_SYMBOLS): confirm the ingestor keeps up + aggregates
   land + bound `trades_raw` retention (currently 30d -> shorten). **Do AFTER market close** so
   it can't destabilize the live executor's EOD flatten.
2. settled-day trade-agg parity (live vs REST) at 50 symbols — QA owns; must pass.
3. design + ship the sharded ingestion (N processes) for the full universe.
4. build order-flow FEATURES into the featurestore (v2 set, parity-checked): OFI (order-flow
   imbalance), signed-volume z (5/15/30m), trade intensity, large-print count, quote imbalance +
   spread dynamics. Test under the COST GATE (net P&L, not IC) on the deep panel.

## Parallel option: delisted-name backfill
To test overnight survivorship-FREE (the overnight result was survivorship). Needs a historical/
delisted asset list (Alpaca trading API doesn't give delisted easily). Weigh vs order-flow —
order-flow is higher-EV (a NEW signal class) than rescuing a likely-dead overnight thesis.
