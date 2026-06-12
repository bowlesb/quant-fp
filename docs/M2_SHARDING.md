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
2. **Alpaca connection limit (THE FORK) — RESOLVED 2026-06-12 via docs (no risky live test needed).**
   Alpaca allows **ONE concurrent market-data websocket connection per account** (Algo-Trader-Plus
   included; a 2nd concurrent connection returns 406/403). So **topology B (multi-websocket) is BLOCKED;
   topology A (one reader + N workers) is REQUIRED, not just preferred.** Silver lining: the paid SIP
   tier has NO channel limit (unlimited trades/quotes/bars channels), so subscribing all ≥500 names'
   trades+quotes on the SINGLE connection is fine — the sole bottleneck is PROCESSING throughput (→ the
   aggregation workers), exactly what topology A shards. The earlier "careful off-hours 2nd-connection
   test" is now UNNECESSARY (don't risk bumping the live ingestor — the docs settle it).
   Sources: docs.alpaca.markets/docs/streaming-market-data, /us/docs/market-data-faq.

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

## 500-name selection — DECIDED (modeller-2, 2026-06-12)
**Pure top-by-ADV from the clean is_etf_like-filtered universe** (modeller-2's call; ADV range rank-1 MU
$47.3B → rank-500 QXO $281M; clean liquidity floor that supports OFI). The LIVE mechanism derives this
DYNAMICALLY from universe_membership by ADV at subscription build time (self-maintains as the universe
churns) — NOT a static committed list; modeller-2's /tmp CSV is the reference snapshot.
RATIONALE (modeller-2): the OFI STREAM (500) and research-panel BREADTH (1000) are DIFFERENT things —
sector-neutral momentum (#20) + dispersion run on the FULL 1000-name panel (price features), so they do
NOT depend on which 500 stream order-flow; pure-ADV costs them nothing. The 500 should optimize for what
it's FOR = OFI SIGNAL QUALITY: OFI (signed-volume imbalance) needs enough trades/minute to be trustworthy,
so a sector-spread cut would pull in low-ADV names where OFI is WEAKEST and where the at-scale parity
re-proof is LEAST likely to hold. Concentration (NVDA/AMD/AAPL-heavy) is fine at the capture tier — book
diversification happens at portfolio construction off the full panel, not here.
V2 (post-pilot, NOT now): if the OFI pilot proves edge AND sector_map (#20) lands, "is OFI
sector-CONDITIONAL?" would want sector coverage in the stream — a deliberate sector-spread expansion gated
on the pilot working. First 500 = max signal quality = pure ADV.
- **Soft boundary → size the cut for EVEN sharding, not exactly 500.** Rank 501 (IR $281.0M) ≈ rank 500
  (QXO $281.2M), so ±20 names costs ~nothing in liquidity. WORKING COUNT: **512 names / 4 shards =
  128/shard** (clean power-of-two; +12 vs 500, within tolerance). Final count tracks the Manager's
  shard-count answer.
- **⭐ Continuity (modeller-2): keep QQQ/SPY as a SEPARATE market-context stream.** Of the current 52
  streamed OF names, 50 are equities (ALL in the top-500 — no OF name dropped by the cut) + QQQ/SPY
  (index ETFs, correctly excluded from the equities universe). When the sharded subscription is rebuilt
  off the EQUITIES universe, QQQ/SPY would silently VANISH — but they're a useful market-beta reference
  for features (never traded). ADOPT: a tiny separate "market-context" subscription (QQQ/SPY/IWM) on the
  reader, routed to its own light path, kept OUT of the equities OFI panel. Prevents silent loss of the
  market-context feed at the rebuild.

## Build plan (Tier-1 PR — the policy's first real one)
1. Verify the Alpaca connection limit (weekend, careful off-hours test).
2. Implement topology A on a role branch → PR; qa reviews data semantics (the aggregation path is the
   parity cornerstone), prod-architect owns runtime/sharding. This PR also shakes down the PR flow.
3. Weekend DRY-RUN at 500 off-hours (or against the weekend's thin feed) — profile per-worker CPU,
   confirm no minute-flush backlog, coverage invariant green.
4. Deploy Monday pre-open (one coordinated restart; the ingestor freeze discipline applies during RTH).
5. qa-2 re-runs the #15 settled-day parity proof at 500 → must hold (99%+ sign agreement) → M2 criterion ticks.

## Manager decisions (APPROVED 2026-06-12) — topology A is the build
- **Topology A approved** (reader + N aggregation-workers; constraint-safe). Still run the Alpaca
  websocket-limit verification (careful, off-hours) so we KNOW whether fallback B is available — but
  it's not blocking, A works either way.
- **Shard count = dry-run-sized, NOT fixed.** 4 workers is the PRIOR; size N from the measured
  per-worker tick-rate budget with ~2× headroom at PEAK (open/close bursts are the binding load, not
  session average). Follow the profile (3/6/whatever it says).
- **Queue substrate = Redis Streams** (in-stack, observable — consumer-group lag is a queryable metric
  that feeds the coverage invariant; decouples reader/worker restart lifecycles). CONDITION: the dry-run
  MUST measure end-to-end queue lag at realistic BURST rates; if serialization can't hold the open's
  tick burst, fall back to multiprocessing queues without ceremony (topology survives the substrate swap).
- **Target = "≥500 up to the liquid cutoff", NOT 500 hard.** ADV-based; floor at 500 to satisfy the
  criterion, ceiling at the liquidity cliff (~500-600); illiquid names fail the cost gate anyway so
  capturing them buys nothing. Size the cut to shard evenly (512/4×128 working).
- **First-real-PR: branch `prod-architect/m2-sharding`; qa-2 is the mapped reviewer** (data semantics —
  aggregation parity is what's at stake; exec not needed, order path untouched). The PR is also the
  first exercise of the full PR flow — its description carries the design rationale so review is substantive.

## Next concrete step
Alpaca websocket-limit verification (careful, off-hours): determine if SIP/Algo-Trader-Plus allows >1
concurrent data websocket per account. RISK to manage: opening a 2nd connection could bump the LIVE
ingestor's single websocket → reconnect loop. Do it deliberately (check Alpaca docs definitively first;
if a live test is needed, account for the ingestor's reconnect and do it when a brief stream gap is
costless — post-close/weekend). Then: #12 completes → build topology A on the branch → weekend dry-run
(profile per-worker CPU + Redis burst lag + coverage invariant green) → Monday pre-open deploy → qa-2
re-proof at the new scale ticks the M2 criterion.
