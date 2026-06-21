# Capture Ceiling — how wide can the live tick subscription go?

**Scoping analysis (Warehouse, 2026-06-22). DOCS/ANALYSIS ONLY — no capture config is changed by this
doc.** Widening the live trade+quote tick subscription (`FP_TICK_SYMBOLS`) is the crystallized TOP
standing infra lever. This doc makes it Ben-actionable: the realistic ceiling, the under-represented
tickers ranked, and a press-the-button phased ramp with per-phase verification.

This resolves the forward-ref in `docs/FEATURE_PLATFORM.md` **FP1.a** ("a `docs/CAPTURE_CEILING.md` note
with the number"). FP1 = "Sub-minute capture at full-universe scale (raw ticks, no drops)"; this is its
provider-ceiling note, grounded in the M2 sharding build (`docs/M2_SHARDING.md`) and the post-#383 fc
latency reality.

---

## Why this is the top lever — the triple unlock

`FP_TICK_SYMBOLS` gates which symbols the live stream subscribes *trades+quotes* for. Unset, it falls to a
conservative **~24-symbol liquid canary** (`DEFAULT_TICK_SYMBOLS` in `quantlib/features/real_capture.py`:
SPY/QQQ/IWM/AAPL/MSFT/NVDA/TSLA/… 24 names). The full **bars** stream already covers the ~11k universe; only
the *tick* (trade+quote) tier is pinned at the canary floor. Widening it is a **triple unlock**:

1. **Universe tick/quote PARITY.** The 56 currently-open parity defects are ALL the FP_TICK_SYMBOLS
   coverage artifact, not math bugs (`within_day_rootcause.py` §2.4; Parity-12 re-confirmed 06-21): the tick
   groups (`trade_flow`, `quote_spread`, `signed_trade_ratio`, `inter_arrival`, `tick_runlength`,
   `trade_size_dist`, `volume_exhaustion`, `microstructure_burst`, …) are backfilled across ~1268 symbols
   but captured LIVE on only the ~24 canary → the rest have NO live data to parity-check, so they sit
   DIVERGENT-by-absence. There is no parity-code fix; the residual is structurally unverifiable until the
   live subscription widens. Widening *is* the fix → those defects clear and the microstructure groups can
   earn trust.

2. **Speculative pre-compute VALUE (#378 / `docs/SPECULATIVE_PRECOMPUTE.md`).** Ticks stream into the
   shard worker's `trade_buf`/`quote_buf` DURING the minute; the official OHLCV bar is the late input
   (≈T+60+δ). The worker is idle on `queue.get()` in between, so ~98% of any tick-derived feature's inputs
   are in at T−ε. The safe speculative form (aggregate ticks early, emit window sums at bar over the
   identical buffer) is value-identical (0.0 breach over a 60-min soak; the unsafe subtract-expiring variant
   was prototyped and REJECTED). But the prize is proportional to **how many symbols stream ticks** — at 24
   syms it saves sub-ms; it becomes meaningful only at universe breadth. #378's own conclusion:
   design-now / build-later **behind this widening**.

3. **The whole microstructure feature class.** Every tick-derived group is honest-null off the subscribed
   set (we never fabricate all-zero tick features). Widening lights up the entire order-flow / microstructure
   surface across the tradeable universe — the substrate for the cost-model (#271, productionized) and any
   future quote-dynamics work, on the names a strategy can actually trade.

The reason it has stayed "crystallized but not pressed": **it is a LIVE-capture subscription change** — a
restart of the live `feature-computer` with a wider `FP_TICK_SYMBOLS`. That is Ben's (or the Lead's) click,
gated on confidence it won't dent live capture. This doc supplies that confidence.

---

## 1. THE CAPTURE CEILING — how many symbols can we tick-subscribe?

Three independent constraints. Two are already resolved in our favor; the third (processing headroom) sets
the practical ramp.

### 1.1 Alpaca connection / subscription limit — RESOLVED, not binding

`docs/M2_SHARDING.md` §"The load-bearing constraint" settled this from Alpaca's docs (no risky live test):

- Alpaca allows **ONE concurrent market-data websocket per account** (a 2nd concurrent connection returns
  406/403). Our architecture already respects this: **one reader process owns the single websocket**
  (`services/ingestor/main.py`; `quantlib/features/real_capture.py` `run_real_capture`).
- The paid **SIP tier has NO channel limit** — subscribing trades+quotes for ALL ~11k names on the single
  connection is allowed. **The connection limit does NOT cap the subscription breadth.**

> **The 06-17 fc-outage lesson (`docs/OPERATIONS.md` §2) is about CONNECTION COUNT, not breadth.** That
> outage was a *reconnect* tripping the single-connection limit: fc's just-dropped socket lingered on
> Alpaca's side, and on reconnect Alpaca rejected the 2nd (apparent) connection → `connection limit
> exceeded` → crash-loop. The fix was operational (`nightly_relaunch.sh` clean-recreate releases the old
> socket cleanly; the `stream_supervisor.py` now self-heals a clean `run()`-return reconnect). This is
> orthogonal to how many symbols are subscribed — widening FP_TICK_SYMBOLS does NOT add connections (still
> one websocket, more channels on it). The lesson it DOES carry into the ramp: a widening = a fc relaunch,
> and a relaunch must go through `nightly_relaunch.sh` (clean socket release), never `docker restart`.

**Verdict: the connection limit is not the ceiling.** One websocket carries the full universe's
trades+quotes.

### 1.2 Bandwidth / message rate — sized, not binding

The SIP firehose at full universe is heavy but bounded. M2's design estimate: ~50 tick-names already produce
tens of thousands of raw ticks/min; the full liquid universe is ~millions of ticks/min at the open burst.
The reader's residual per-tick cost is **one md5-hash + a queue append** to route each raw tick to its shard
(the reader NO LONGER aggregates — see §1.3). Receive-only is ~10× cheaper than aggregate. The M2 dry-run
pushed **102,400 trades + 102,400 quotes in a single open-minute burst** (512 names × 200 ticks each)
through the real reader→4-worker path with **zero loss**. Network/receive was never the bottleneck in any
measurement; processing was.

### 1.3 fc shard-worker processing headroom — THE real (and ample) constraint

This is what actually sets the ramp, and the architecture was built precisely to make it scale:

- **Tick aggregation runs on the SHARD WORKER that owns each symbol, not inline on the reader**
  (`real_capture.py` lines 49-56). The reader routes raw ticks by `hash(symbol) % n_shards`; each worker
  aggregates its own shard's `TickState` (sign classification, spread/imbalance, the raw trades frame).
  The firehose is *distributed across the worker pool* — exactly the topology-A design M2 approved.
- **Shard count defaults to `cpu_count // 4`** (`real_capture.py:199`) = **8 shards on the 32-core box**,
  with `POLARS_MAX_THREADS` pinned to `cpu_count // n_shards` so the workers don't thrash cores. 8 shards
  is the *measured* compute-only optimum at 10k/32-cores (617ms p99; 10 shards=661, 6=696).
- **The M2 dry-run is the load-bearing evidence:** 512 tick-names' full open-minute burst aggregated across
  **4 worker procs in 4.35s** — **>13× headroom** under the 60s minute budget. With the production default
  of 8 shards on 32 cores, the per-shard tick load at 512 names is half that again.
- **Post-#383 latency is honestly ~2-8 ms/group** (the dashboard now reports the true per-group compute,
  not profiler artifacts; the e2e gate on 06-21 measured p50 262 / p99 304 ms end-to-end at 256 syms/8
  shards, both well under the 320/420 ceilings). The bars path already runs the full ~11k universe every
  minute inside budget; adding tick aggregation for a subset is incremental per-shard work the dry-run shows
  is cheap.

### 1.4 The number / range

| Tier | Symbols | Basis | Status |
|---|---|---|---|
| **Canary (current)** | **~24** | `DEFAULT_TICK_SYMBOLS`, FP_TICK_SYMBOLS unset | LIVE today |
| **Proven-safe** | **~512** | M2 dry-run: 512 names, full open burst, 4.35s/4 workers, zero loss | DRY-RUN VERIFIED, never deployed |
| **Safe target (recommended)** | **~1000–1500** | liquid B1+B2(+B3 head); 8 shards on 32 cores ≈ 2× the dry-run per-shard load, still inside the >13× headroom | the recommended end-state for tick parity + microstructure trust |
| **Hard ceiling** | **~the full ~11k universe** | SIP has no channel limit; one websocket; processing is the only governor and the bars path already runs 11k/min in budget | reachable but unnecessary — the illiquid 4k+ tail trades too rarely for tick features to be useful (honest-null is fine) |

**Concrete answer:** **~512 symbols is proven-safe today** (dry-run evidence in hand). **~1000–1500 (the
liquid B1+B2 + B3 head) is the recommended safe target** — it covers every name a strategy realistically
trades and where tick/order-flow features carry signal, with comfortable per-shard headroom. The **hard
ceiling is the full ~11k universe** (no connection/bandwidth wall; processing is the governor and the bars
path already proves 11k/min fits), but the illiquid 4k+ tail buys little — those names trade too sparsely
for trustworthy tick aggregates, so leaving them honest-null is correct, not a gap.

---

## 2. UNDER-REPRESENTED TICKERS — who lacks tick coverage, and the priority order

Today the live tick set = the ~24 canary. **Everything else in the universe is under-represented LIVE** for
the tick groups (present in backfill across ~1268+ symbols, absent in the live stream). The priority for
widening is liquidity-weighted: tick/order-flow features are only trustworthy where there are enough
trades/minute, and those are exactly the names a strategy trades.

### 2.1 The shared liquidity bands (every lane uses these)

From `docs/TICKER_REPRESENTATION.md` / `quantlib/data/b4_quote_widen.py`, ADV-ranked (trailing-20d RTH
dollar volume, point-in-time):

| Band | ADV rank | Names | Tick-feature value | Widening priority |
|---|---|---|---|---|
| **B1** | top-500 | 500 | HIGHEST — dense trades/min, OFI trustworthy | **1st** |
| **B2** | 500–1k | 500 | HIGH | **2nd** |
| **B3** | 1k–2k | 1000 | MODERATE (head of B3 still useful) | 3rd (head) |
| **B4** | 2k–4k | 2000 | LOW — sparse trades, weak OFI | opportunistic |
| **B5** | 4k+ | ~2593 | NEGLIGIBLE — illiquid, honest-null correct | skip |

### 2.2 The priority ordering for widening

1. **B1 (top-500) first.** This is exactly where the M2 OFI selection already landed ("pure top-by-ADV",
   modeller-2, 2026-06-12) — the 512-name dry-run set IS essentially B1. Maximum signal quality, maximum
   parity-clearing value (clears the most-tradeable microstructure cells), and dry-run-proven safe.
2. **B2 (500–1k) second.** Doubles the tradeable tick surface; still inside processing headroom at 8 shards.
3. **B3 head (1k–~1500) third, optional.** Diminishing tick-signal value; include if a specific
   microstructure feature wants the breadth.
4. **B4/B5 — do NOT widen for ticks.** These already have the known *quote-tape backfill* gap
   (`docs/TICKER_REPRESENTATION.md`: 106 zero-quote B4 + 2327 zero-quote B5), but for LIVE tick subscription
   they buy nothing — too few trades/min for trustworthy aggregates. Honest-null is the correct state.

> The live mechanism already derives the subscribed set **dynamically by ADV** at subscription-build time
> (`route_minute`/`tick_symbols` + the M2 dynamic top-N-by-ADV from `universe_membership`), so a band-based
> target self-maintains as the universe churns — no static committed list to rot. Setting
> `FP_TICK_SYMBOLS=all` subscribes the whole universe; a bounded ramp instead sets it to the explicit
> B1(+B2) list (or, preferably, wires the existing dynamic top-N ADV selector that M2 built).

### 2.3 Market-context continuity note

Keep **SPY/QQQ/IWM** in the subscribed set regardless of band (they're index ETFs excluded from the equities
ADV universe but used as market-beta references). They're already in `DEFAULT_TICK_SYMBOLS` and M2's
"market-context stream" note covers this — don't let them vanish when the set is rebuilt off the equities
universe.

---

## 3. THE WIDENING PLAN — phased FP_TICK_SYMBOLS ramp (press-the-button runbook)

A phased ramp so each step is verified no-dent before the next. **Every phase = a fc relaunch via
`nightly_relaunch.sh` (clean socket release per the 06-17 lesson), NEVER `docker restart`.** This is Ben's /
the Lead's live-capture click; the steps below are the runbook for when it's prioritized.

### Per-phase invariants (apply to every phase)

- **Relaunch, never restart.** Set `FP_TICK_SYMBOLS` for the new phase, then
  `UNIVERSE_MAX_SYMBOLS=100000 ops/nightly_relaunch.sh $(date +%F)`. The relaunch `docker rm -f`'s the old
  fc (cleanly releasing its Alpaca websocket → no lingering-socket reconnect trip), reseeds, and recreates
  with `FP_WARM_START=1`. Do it pre-open or off-hours so any brief gap is costless.
- **Verify capture is actually live** (`docs/OPERATIONS.md` §1: "Up" ≠ "capturing") — check **data on
  disk / the bus**, not container state:
  - `docker logs feature-computer --tail 30 | grep -E 'day=|connection limit'` (clean reconnect, correct day)
  - stream partitions written for today: `ls -d /store/group=*/v=*/source=stream/date=$(date +%F) | wc -l`
  - fresh bus vectors: `redis-cli XREVRANGE fv:AAPL + - COUNT 1` (~now)
- **Verify the bars path did NOT regress.** The widening must not dent the universe-wide bars compute. Watch
  the per-minute compute_ms (the e2e latency gate / dashboard) stays under the 320/420 ms ceilings and no
  minute is missed.
- **Verify the coverage invariant** (M2 `coverage.py`): per-shard `ingestor_shard_coverage_ratio` ≈
  streamed/subscribed; alarm if a subscribed liquid name goes silent >K RTH minutes.

### The crypto-canary same-box pre-check (do this BEFORE any equity phase)

The 24/7 `crypto-capture` runs the SAME engine on the SAME box (`docs/CRYPTO_E2E.md`). The RTH-dent gate was
already CLEARED this way (06-20: 129 bounded compares concurrent with live capture → no compute_ms spike, no
missed minute, no restart). Use it as the same-box contention canary for the widening:

- Off-hours, drive the crypto fc with a WIDENED tick set (more crypto pairs / a synthetic-heavy tick load
  bounded to the crypto root) and confirm **no compute_ms spike, no missed minute, no restart** under the
  added per-minute tick-aggregation load.
- A clean crypto canary = evidence the equity box has the per-minute headroom for the next equity phase,
  proven without touching live equity capture. (Crypto writes its OWN store root / bus namespace — zero
  equity cross-contamination.)

### The phases

| Phase | `FP_TICK_SYMBOLS` | Count | Per-phase risk | Verification gate |
|---|---|---|---|---|
| **P0 (today)** | unset | ~24 canary | none | baseline: capture live, bars within budget |
| **P1** | B1 list (or dynamic top-500 ADV) | ~500 | the dry-run-proven safe step; conn-limit N/A (one websocket); per-shard load = the 4.35s/4-worker dry-run | crypto-canary clean → relaunch → capture-live checks + coverage invariant green + bars compute_ms unchanged for a full RTH session + the tick groups' live parity defects start clearing |
| **P2** | B1+B2 (top-1000 ADV) | ~1000 | 2× P1 per-shard tick load; still inside 8-shard headroom; watch open-burst compute_ms | re-run P1 gates a full session; confirm p99 compute_ms still < 420; confirm no shard coverage alarm at the open burst |
| **P3 (optional)** | + B3 head (~1500) | ~1500 | diminishing tick-signal value; only if a feature wants it | same gates; STOP here unless a concrete need pulls further |
| **Ceiling (not recommended)** | `all` | ~11k | processing only (no conn/bandwidth wall); the 4k+ tail is honest-null-better | only if a universe-wide tick study is ever justified; the bars path proves 11k/min fits, but tick aggregates on the illiquid tail are untrustworthy |

**Rollback at any phase:** revert `FP_TICK_SYMBOLS` to the prior phase's value and `nightly_relaunch.sh`
again — the subscription set is a single env var, the relaunch is idempotent, and the prior phase was
already proven. No data is lost (the bars path is unaffected; tick groups simply return to honest-null on the
removed names).

**Recommended landing point: P2 (~1000, the liquid B1+B2 core).** It clears the tick-parity defects on
every tradeable name, lights up the microstructure feature class where it carries signal, and makes the
speculative pre-compute prize (#378) meaningful — all inside proven processing headroom. P3/ceiling are
available but unmotivated for now.

---

## 4. What this unblocks once pressed

- **Parity:** the 56 FP_TICK_SYMBOLS coverage-artifact defects clear (live data finally exists to compare).
  The Parity lane resumes on real tick data instead of being structurally blocked.
- **Trust:** the microstructure / order-flow groups become eligible to earn binary trust across the
  tradeable universe (within-day cert + nightly sweep now have live tick values to grade).
- **Latency:** speculative pre-compute (#378) becomes worth building — the tick-reduction targets move
  off the at-bar critical path at universe breadth, where the saving is real.
- **Features:** the whole microstructure substrate goes live for the Modeller's order-flow / cost-model
  work on the names that matter.

The ceiling is not the constraint; the **decision** is. This doc turns the lever from "crystallized" into
"press-the-button when Ben prioritizes it."
