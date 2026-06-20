# LIQUIDITY-PROVISION SURFACE — earn-the-spread (PRE-REGISTRATION + DESIGN)

**Author:** Modeller · **Date:** 2026-06-20 · **Status:** PRE-REGISTERED DESIGN — written BEFORE any
outcome. ⚠️ The FILL MODEL below is pre-committed in full BEFORE any return is computed, so it cannot be
tuned to a result (the coordinator's load-bearing note: the honest fill model is the whole ballgame). The
Lead reads this BEFORE I run it.

## WHY THIS SURFACE — the one framing where cost is the EDGE, not the enemy

8 settled negatives, all sharing one killer: we PAID the spread (a liquidity TAKER), and at our scale the
spread eats the signal. Liquidity PROVISION inverts it — a passive limit order at/inside the touch EARNS
the spread when it fills. The deepened quote tape (now queryable: tick bid/ask + sizes, 379d 2024-12-12→
2026-06-18, 4,300 names, liquid core complete, e.g. PLTR ~1.16M quotes/day) makes an HONEST simulation
possible for the first time — the exact data we lacked when I DEFERRED this avenue.

## ⚠️ THE FILL MODEL IS THE WHOLE EXPERIMENT — frozen, falsifiable spec (written as an EXACT rule)

A naive LP backtest assumes "I post at the bid, I get filled, I earn the spread" — FANTASY. The exact rule
below is FROZEN before any P&L is computed. Fills are constructed off REAL TRADE PRINTS (not inferred
displayed-size decay) — no bar-only fill assumptions anywhere.

### FEED NATURE (small ask #2 — stated up front, bounds the queue realism)
The quote stream is the **consolidated SIP NBBO** (Alpaca's SIP-sourced data — verified: each tick names the
posting exchange, 15 distinct bid/ask exchange codes, tape C). So `bidsz`/`asksz` is the AGGREGATE displayed
size at the national best bid/offer across venues, not a single book. Consequence for the queue model: we
place ourselves at the NBBO touch and reason about aggregate FIFO, but we do NOT see our position within any
one venue's book — so the back-of-queue FIFO is an approximation against the aggregate touch size, NOT a
literal single-venue queue. Stated so the reader knows how literally to take the queue fraction (we lean
pessimistic precisely because aggregate-FIFO cannot model how far back a real order sits in a specific venue).

### EXACT FILL RULE — off REAL TRADE PRINTS (the required hardening, frozen)
We replay each name's tick quote stream `{t_k: bid_k, bidsz_k, ask_k, asksz_k}` JOINED to the **real trade
tape** `{ts, price, size}` (379d / complete breadth / SAME 2024-12-12→2026-06-18 window as quotes — verified
present, 7,688 names). We continuously post a passive BUY at the prevailing best bid `B` (SELL at best ask
`A` mirrors); the BUY leg:
1. **Post** at price `B` at tick `k` → queue-ahead `Q0 = bidsz_k` (aggregate displayed size at `B`).
2. **A fill requires a REAL TRADE PRINT at/through our level.** Walk forward; our resting BUY at `B` accrues
   `traded_through = Σ size` of every trade print with `price <= B` that occurs while the NBBO bid is still
   at/above `B` (a sell-initiated execution hitting our price). This is the ACTUAL executed size + the ACTUAL
   fill TIMING (the print `ts`) — NOT displayed-size decay. **This removes the cancel/trade confound:** a
   cancellation shrinks `bidsz` but generates NO print, so it never triggers or mis-times a fill. (The
   earlier displayed-decay proxy is REJECTED per the design audit — decay conflates cancels, which DOMINATE
   on lit quotes, with trades, and can manufacture a FALSE-POSITIVE median because a phantom cancel-"fill"
   carries a non-trade markout the median gate cannot catch. Prints only.)
3. **Queue-position fill fraction (frozen):** `fill_frac = clip( (traded_through − Q0) / OUR_SIZE , 0, 1 )`
   for back-of-queue (we fill only the residual after the `Q0` of real prints ahead of us execute); a
   mid-queue sensitivity uses `(traded_through − Q0/2)`. **Verdict uses BACK-OF-QUEUE.** `OUR_SIZE` per the
   capacity section. The fill TIME = the print `ts` at which cumulative `traded_through` crosses `Q0` (real
   timing, from prints).
4. **Fill price = `B`** → earn `(mid − B)/mid` = the half-spread vs the contemporaneous mid at the fill
   print. If `traded_through` never exceeds `Q0` within a max-resting horizon `R` (frozen `R = 30 min`),
   CANCEL — no fill, no P&L, idle time recorded (opportunity cost / capacity drag).

### (B) ADVERSE SELECTION — calibrated to OBSERVED post-fill drift, NOT a free parameter
The adverse-selection cost is NOT a tunable haircut — it is MEASURED from the tape: for every simulated
fill at tick `j` (price `B`, mid `m_j`), the realized markout = `(m_{j+H} - m_j) / m_j` for `H in {1,5,15}`
min (a BUY fill profits if the mid RISES, loses if it falls). The post-fill mid path is the REAL future
quote mid — never assumed zero. **Per-fill P&L = half-spread-earned `(m_j - B)/m_j` + markout `(m_{j+H} -
m_j)/m_j` − exit cost (C).** Because LP fills are adversely selected, the markout is expected NEGATIVE for a
BUY (we filled because it kept falling); the question the data answers is whether `half-spread > |markout| +
exit`. The adverse-selection magnitude is thus an OUTPUT (the observed markout distribution), pre-registered
to be reported per name + pooled, NOT a parameter I set. Every fill counts (no survivorship over fills).

### (C) INVENTORY / EXIT — no costless unwind (frozen)
The filled long is flattened at horizon `H` by a TAKING exit = pay the half-spread to hit the bid
(`exit_cost = (A_{j+H} - m_{j+H})/m_{j+H}` = the contemporaneous half-spread). Verdict uses the taking exit
(conservative — passive exit only helps, tested as a sensitivity). Inventory capped at `MAX_POS` per name;
at the cap we stop posting that side. So the strategy must earn the entry half-spread NET of the realized
adverse markout AND a full exit half-spread — the honest hurdle.

## CAPACITY / SCALE HONESTY (pre-committed — ask #4)
At our size, LP only "clears" where our order is small vs displayed depth AND fills are frequent enough to
matter. Pre-registered reporting, per name:
- `OUR_SIZE` is pinned to a FIXED notional (frozen: **$10k per quote**, ~Ben's real-money unit) converted to
  shares at the contemporaneous mid — NOT a free lot count. This sets our queue weight realistically.
- Report per name: median displayed depth (so `OUR_SIZE`/depth = our queue weight), fills/day under the
  back-of-queue rule, the CAPTURED-SPREAD FRACTION = (realized half-spread earned net of markout) / (quoted
  half-spread) — the honest fraction of the spread we actually keep after queue + adverse selection.
- The verdict universe = names where fills/day ≥ a frozen floor (enough events to be non-anecdotal) AND
  `OUR_SIZE` ≤ the displayed depth (we don't move the market). Names failing either are reported as
  "no capacity" not "no edge." Expectation: only the tightest, deepest mega-caps even qualify — and those
  have the SMALLEST spread to earn (the capacity/edge tension is itself the finding).
- **FILL-RATE SANITY CHECK (small ask #1 — a phantom-fill diagnostic):** report per name our simulated
  filled SHARES/day as a fraction of the name's ACTUAL total traded volume/day. A real LP at $10k/quote on a
  mega-cap should capture a SMALL fraction of daily volume; if the sim fills materially more than realistic
  participation (e.g. > a few % of daily volume), that is the phantom-fill problem surfacing (we'd be
  "filling" more than the tape supports) — a visible red-flag diagnostic, reported alongside the verdict,
  not buried. With the trade-print join this should be self-limiting (we can't fill more than printed), but
  it is reported as the explicit check that the join is doing its job.

## HYPOTHESES (pre-registered — 2)

### H1 — PASSIVE SPREAD CAPTURE on the most liquid names is net-positive after honest fills
**Claim:** continuously quoting both sides (post at bid + ask) on the tightest, deepest names earns the
spread faster than adverse selection + the taking-exit erodes it → net-positive per-fill P&L.
- Verdict metric: mean AND MEDIAN net P&L PER FILL (in bps of mid) after (A) back-of-queue fill prob, (B)
  real post-fill adverse mid-move over H, (C) taking exit. Plus net P&L per unit time (fills are sparse).
- Pre-committed prior: SKEPTICAL. Uninformed two-sided quoting is the textbook LOSER (adverse selection ≈
  the spread for an uninformed provider). The honest question is whether on the very tightest mega-caps
  there's residual edge, OR whether a CONDITIONING signal (H2) is required.

### H2 — CONDITIONAL provision: quote only when adverse-selection risk is LOW
**Claim:** provision is net-positive only when conditioned — quote the bid only when short-horizon flow/
imbalance suggests the next move is NOT down (reduce the adverse-fill rate). Signal candidates (all from the
quote stream, point-in-time): quote imbalance (bid_size vs ask_size), recent mid drift, recent spread
regime. Quote one side only when its adverse-selection proxy is favorable.
- Verdict: same net-per-fill metric, conditioned; must beat H1 (unconditional) AND clear zero net-median.
- Pre-committed prior: this is where LP edge lives IF anywhere — but the conditioning signal is itself
  tested for look-ahead (point-in-time, entered after the quote it reads) + shuffle.

## DISCIPLINE (the full spine + LP-specific)
- POINT-IN-TIME: every quote/fill decision uses only quote data with `ts <= decision_time`; the post-fill
  adverse move uses STRICTLY FUTURE mids. No look-ahead in the fill OR the signal.
- The fill model (A/B/C) is FIXED as written above before any P&L is computed — no post-hoc tuning of
  fill-prob / queue / adverse-horizon to a result.
- SHUFFLE baseline: shuffle the fill-timestamp→future-mid linkage (break the adverse-selection structure);
  a real spread-capture edge should SURVIVE (the spread earned is structural), while a fake edge from a
  lucky adverse-path vanishes. Predict-zero = the "quote nothing" baseline (0 P&L, 0 risk).
- NET of all three frictions; reported per-fill mean + MEDIAN + per-day + fills/day (capacity).
- OOS: split the 379d quote window early/late; sign-consistency.
- $1-floor; cap to the liquid core first (where fills are real), report breadth.
- Multiple-comparisons (BY-FDR) across (hypothesis × horizon × queue-setting).

## KILL CONDITIONS (pre-committed, median-anchored — ask #2; same discipline that just settled #205)
The verdict statistic is the **net P&L PER FILL, MEDIAN, in bps of mid**, on the capacity-qualifying
universe, under back-of-queue fills + observed-markout adverse selection + taking exit. Stated in advance:
- **SETTLES AS NULL** (the expected, textbook outcome) iff the net-per-fill **MEDIAN ≤ 0** on the verdict
  setting — REGARDLESS of the mean. A favorable MEAN (a few lucky non-adverse fills) with median ≤ 0 = LP
  doesn't clear adverse selection → null, surface settled, written and closed. A better mean CANNOT reopen
  it (exactly the #205 trap the median gate just caught).
- **REOPENS / promotable** iff the net-per-fill **MEDIAN > 0** AND it is OOS-consistent (early/late 379d
  split, same sign) AND survives the fill-shuffle (the markout-link permutation) AND holds at the
  back-of-queue (pessimistic) setting, on a capacity-qualifying universe with non-anecdotal fills/day →
  then and only then FLAG the Lead for confirmatory replication BEFORE any excitement. The bar is HIGH on
  purpose — LP backtests are the easiest of all to fool.
- **NON-ROBUST** (reported, not promoted) iff a positive median appears ONLY at an optimistic fill/queue
  setting (mid-queue, passive exit), or ONLY on names below the capacity floor, or ONLY in one OOS half.

## RUN PLAN (gated on the Lead's read of THIS AMENDED pre-reg)
Build (`build_fills.py`) JOINS the per-name quote stream to the real trade tape (both 379d, same window) →
simulates two-sided (H1) / conditional (H2) quoting under the trade-print fill rule, emitting a per-fill
ledger (fill ts from the print, side, price, Q0 displayed size, traded_through, post-fill adverse mid path,
exit). Screen (`screen.py`) = the net-per-fill MEDIAN gate + shuffle + OOS + FDR + the fill-rate-vs-volume
sanity diagnostic. Reuses the #205/#212 host-mounted resumable cache + chunked-subprocess infra (the
quote+trade replay is heavier than the bar build → chunk by name/day is essential). Research-only: NO
quantlib / NO fingerprint; READ-ONLY stores; bounded NAMED `--rm` sandboxes (kill by ID).

**FALLBACK (only if the trade↔quote ts alignment proves harder than expected mid-build):** run the
displayed-decay proxy BUT (a) label any positive median PROVISIONAL + non-promotable until the trade-print
rejoin, and (b) report the cancel/trade ambiguity as a first-class FALSE-POSITIVE caveat (NOT as
conservatism). The real print join is strongly preferred and is the plan; the tape is verified present so I
expect the join to land cleanly.
