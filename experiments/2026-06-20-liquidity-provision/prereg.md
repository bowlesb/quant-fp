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
below is FROZEN before any P&L is computed. NO bar-only fill assumptions anywhere — every fill decision is
off the tick NBBO quote stream.

### EXACT FILL RULE (the simulation, step by step)
We replay each name's tick quote stream `{t_k: bid_k, bidsz_k, ask_k, asksz_k}`. We continuously post a
passive BUY at the prevailing best bid `B` (and symmetrically a SELL at the best ask `A`); consider the BUY
leg (the SELL is the mirror):
1. **Post** at price `B` at tick `k`, joining the BACK of the FIFO queue → queue-ahead `Q0 = bidsz_k`
   (the displayed size already resting at `B`).
2. **A fill event requires the touch to come to us.** Walk forward tick by tick. Our resting BUY at `B`
   gets trade-through only when the best bid DROPS to `<= B` later — operationally, the first tick `j>k`
   where `bid_j <= B` AND the level `B` is being consumed. Since we lack the trade-print tape joined yet,
   the pre-committed conservative PROXY for "size traded through our level" in the interval is the DECREASE
   in displayed size at our price plus the size that disappeared when the bid ticked down through `B`:
   `consumed = max(0, Q0 - bidsz_at_B_now) + (bidsz_when_bid_breaks_below_B)`. This is the size that left
   the `B` level — an upper bound on what traded, so it OVER-states our fill chance (conservative AGAINST a
   null, i.e. it can't manufacture a fake null; if anything it's generous to LP, and we still expect LP to
   lose — making a null robust).
3. **Queue-position fill fraction (frozen):** `fill_frac = clip( (consumed - Q0) / OUR_SIZE , 0, 1 )` for
   back-of-queue (we fill only AFTER the `Q0` ahead of us is consumed); a mid-queue sensitivity uses
   `(consumed - Q0/2)`. **Verdict uses BACK-OF-QUEUE.** `OUR_SIZE` is pinned per the capacity section.
4. **Fill price = `B`** (we earn `mid - B` = the half-spread at fill vs the contemporaneous mid). If the
   bid never reaches us within a max-resting horizon `R` (frozen `R = 30 min`), the order is CANCELLED —
   no fill, no P&L, capital idle (we record the idle time = the opportunity cost / capacity drag).

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

## RUN PLAN (gated on the Lead's read of THIS pre-reg)
Build (`build_fills.py`) replays the per-name quote stream → simulates two-sided (H1) / conditional (H2)
quoting under the fill model, emitting a per-fill ledger (fill ts, side, price, displayed size, post-fill
adverse mid path, exit). Screen (`screen.py`) = the net-per-fill median gate + shuffle + OOS + FDR. Reuses
the #205/#212 host-mounted resumable cache + chunked-subprocess infra (the quote replay is heavier than the
bar build, so chunking by name/day is essential). Research-only: NO quantlib / NO fingerprint; READ-ONLY
stores; bounded NAMED `--rm` sandboxes (kill by ID). I send the Lead this pre-reg for read BEFORE building.
