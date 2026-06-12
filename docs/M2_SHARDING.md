# M2 sharded trade/quote ingestion (50 → ≥500) — SCOPE / SHARD-COUNT plan

Owner: prod-architect (M2 critical-path build). Status: DESIGN CHECKPOINT for the Manager (sign-off
gates the build). Requirements partner: modeller-2 (500-name selection). Reviewer: qa (data semantics,
Tier-1 PR). Target: dry-run weekend → deploy Monday pre-open → qa-2 settled-day re-proof at the new
scale ticks the M2 criterion. Green-lit by #15 passing at 50 (sign-agreement 99.85%).

## Goal
Stream trades+quotes for ≥500 liquid equities (up from 50) with NO loss of aggregation fidelity (the
#15 parity must re-pass at 500) and a LIVE COVERAGE INVARIANT built in from day one — streamed==subscribed,
alarmed — so a capture regression at scale is caught immediately, not discovered in a backfill diff.

## The load-bearing constraint (decides topology) — VERIFY FIRST
Current ingestor = ONE single-threaded asyncio process, ONE `StockDataStream`, subscribing
bars(1000)+trades(50)+quotes(50). Two things scale-break at 500 trade/quote names:
1. **Processing throughput.** 50 names already ≈ tens of thousands of raw ticks/min; 500 ≈ ~10× →
   millions of ticks/min. A single Python asyncio process doing per-minute quantlib aggregation over
   all of it becomes CPU-bound; a slow minute-flush backs up the websocket receive loop → dropped
   messages → coverage loss. This is the real bottleneck (receiving is cheap; aggregating is not).
2. **Alpaca connection limit (THE FORK).** STATE.md flags "single data websocket per account." If that
   is a HARD Alpaca limit, we cannot shard by multiple websocket connections on one key. **First
   design task: verify whether Algo-Trader-Plus/SIP allows >1 concurrent data websocket per account**
   (check Alpaca docs + a careful weekend test that does NOT contend with the live ingestor — e.g. a
   throwaway 2nd connection off-hours). The answer picks the topology:

## Topology options
**A. Reader + aggregation-workers (SAFE DEFAULT — works regardless of the connection limit).**
One lightweight reader process owns the single websocket (receive + route only, no aggregation), fans
ticks to N worker processes by symbol-hash via a queue (Redis Streams — already in the stack, or
multiprocessing). Each worker owns a symbol shard, does the CPU-heavy quantlib aggregation + DB writes
for its shard. Scales by adding workers; the reader stays light. Pro: one websocket (constraint-safe),
clean shard isolation, no aggregation cross-talk (tick_state is per-symbol → trivially shardable). Con:
the reader is a single point for receive throughput (but receive-only is ~10× cheaper than aggregate).

**B. Multi-process / multi-websocket (SIMPLER, only if the account allows N connections).**
N independent ingestor processes, each its OWN websocket subscribing to a ~(500/N)-name shard, each
aggregating its own shard. Pro: fully horizontal, no shared reader, each process is today's ingestor
with a smaller symbol set. Con: requires multiple data websockets per account — BLOCKED if the limit
is hard.

**RECOMMENDATION:** design for **A** (reader+workers) as the default — it's correct under either
connection limit and the per-symbol `tick_state` makes symbol-hash sharding clean. If the weekend
verification proves multiple websockets are allowed, **B** is a simpler fallback we can adopt. Either
way: **shard count = ceil(500 / ~125 names/worker) ≈ 4 workers** to start (sized so one worker's
per-minute aggregate finishes well inside the minute with headroom; tune from the dry-run CPU profile).

## Live coverage invariant (built in, not bolted on)
Per shard, each minute: assert `symbols_with_a_tick_this_minute ⊇ subscribed_symbols_expected_to_trade`
(allowing genuinely no-trade illiquid minutes). Emit a per-shard coverage gauge (streamed/subscribed)
to Prometheus; ALARM when a subscribed liquid name goes silent for >K minutes during RTH (the
capture-regression signal qa-2 watches). This is the acceptance gate at each scale step.

## 500-name selection (coordinate with modeller-2)
Top-500 by ADV$ from the CURRENT clean universe (the same is_etf_like-filtered equity set #1 produced).
modeller-2 owns whether it's pure ADV or ADV-with-a-sector-spread for cross-sectional breadth — that's
a research-relevance call. Prod provides the ranked list; modeller-2 signs off on the 500 cut.

## Build plan (Tier-1 PR — the policy's first real one)
1. Verify the Alpaca connection limit (weekend, careful off-hours test).
2. Implement topology A on a role branch → PR; qa reviews data semantics (the aggregation path is the
   parity cornerstone), prod-architect owns runtime/sharding. This PR also shakes down the PR flow.
3. Weekend DRY-RUN at 500 off-hours (or against the weekend's thin feed) — profile per-worker CPU,
   confirm no minute-flush backlog, coverage invariant green.
4. Deploy Monday pre-open (one coordinated restart; the ingestor freeze discipline applies during RTH).
5. qa-2 re-runs the #15 settled-day parity proof at 500 → must hold (99%+ sign agreement) → M2 criterion ticks.

## Open questions for the Manager (design checkpoint)
- Shard count: 4 workers (≈125/shard) to start — OK, or size to a specific per-worker budget?
- Queue substrate for topology A: Redis Streams (in-stack) vs multiprocessing — preference?
- Is 500 the hard target or "≥500 up to the liquid cutoff"? (affects the ADV cut with modeller-2)
- Confirm the first-real-PR-flow intent (qa as data-semantics reviewer) so I branch accordingly.
