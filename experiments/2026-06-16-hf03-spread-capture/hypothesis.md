# HF03 — Spread-capture / liquidity-provision: EARN the spread (invert the cost adversary). Pre-registration.

**Registered:** 2026-06-16, BEFORE any run. Follow-up to HF01+HF02 (both KILLED: qimb is a real but
too-faint directional signal — the per-trade edge does not survive CROSSING the spread). HF03 flips the bet:
instead of PAYING the spread to take, POST at the touch and EARN it — turning the adversary that killed
HF01/HF02 (the spread) into the revenue. Still menu #1 (zero-dependency). NO fingerprint/feature changes.

## The inversion

HF01/HF02 were TAKER bets: cross the spread (pay ~0.5–2.5 bps half-spread per side) to capture a directional
edge that turned out smaller than the spread. A MAKER bet is the opposite: post a passive bid (and ask),
get filled when someone crosses to you, and EARN the half-spread on each round-trip — PROVIDED you are not
systematically filled on the side that's about to move against you (adverse selection). qimb (the real,
significant IC from HF01/HF02) is exactly the tool to MANAGE that: skew/skip quoting on the side the book
says is about to get run over.

## Hypothesis

A simple passive liquidity-provision policy on megacaps — post at the touch, earn the half-spread on filled
round-trips, and use qimb to AVOID posting on the adversely-selected side — nets POSITIVE per filled
round-trip after realistic adverse-selection costs, with a per-trade bootstrap 95% CI ABOVE zero (the same
decisive gate that killed HF02, now applied to the maker P&L).

## The honest hard part (pre-committed — this is where maker backtests lie)

HF03 is a MODELED backtest with STATED, CONSERVATIVE fill assumptions — NOT a live-fill measurement (Alpaca
paper cannot report queue position or true passive fills, so a live paper run would not validate the fill
model either). The two ways a maker backtest prints fake profit, both pre-committed against:

1. **FILL = TRADE-THROUGH, not touch (queue position is the whole ballgame).** A passive limit at the best
   bid is NOT filled just because the market TOUCHED your price — there are orders AHEAD of you in the queue.
   Pre-committed conservative rule: a passive bid at price P is filled only when a trade prints STRICTLY
   BELOW P (the market traded THROUGH your level, clearing the queue ahead of you), OR conservatively
   require the printed size at/through P to exceed a queue-depth proxy (the prevailing bid_size at the time
   you posted). Report fill rate; this is STILL an upper bound (we can't see true queue), flagged as such.
   A "touched → filled → earned half-spread" rule is the artifact and is FORBIDDEN.
2. **ADVERSE SELECTION via the POST-FILL MARK-OUT (the decisive, honest test).** You are filled preferentially
   when you're WRONG — informed flow hits your bid right before the mid drops. After EACH fill, MARK OUT the
   position at the prevailing MID at +1s, +5s, +30s, +60s. The true per-fill edge =
   (half-spread earned) − (mark-out loss = mid_at_markout − fill_price for a bought bid). A naive
   "+half-spread per fill" with NO mark-out is the artifact. **The headline metric is the MARK-OUT-NET per
   fill distribution, with a per-FILL bootstrap 95% CI (the HF02 discipline). It is a KEEP only if that CI
   excludes zero ABOVE at the conservative trade-through fill model.**
3. **INVENTORY/EXIT:** a filled bid leaves you LONG; model exit at the mark-out horizon (mark at mid =
   passive-mid assumption) AND a cross-exit (pay half-spread). Report both; cap inventory.

## Test design

- Universe + data: the HF01/HF02 deep-quote megacaps (8–9 names, ~63 days), quotes + trades, genuine UTC
  (RESEARCH_PITFALLS #1).
- Baseline maker P&L per filled round-trip = (half-spread earned on entry) − (adverse mid move over the hold)
  − (exit cost: half-spread earned if exited passively / paid if crossed). Hold windows h ∈ {1, 5, 15} min.
- qimb overlay: only post on the bid when qimb is not strongly negative (book not about to drop), only post
  on the ask when qimb is not strongly positive; sweep the qimb threshold. Compare to the no-overlay maker
  baseline — does qimb-conditioned quoting reduce adverse selection enough to net positive?
- **THE DECISIVE GATE (same as HF02):** the PER-FILLED-ROUND-TRIP bootstrap — 10k resamples of the mean net
  P&L per fill; 95% CI must EXCLUDE zero ABOVE for a KEEP. Report n_fills, win rate, per-trade t. Walk-forward
  OOS (day-clustered, the FIXED metric). Canary: a shuffle of the fill-time qimb (does the qimb overlay beat
  a random posting policy?).

## Expected / confidence

- Confidence the qimb-conditioned maker nets positive per fill OOS with the bootstrap CI above zero: **~20%.**
  Adverse selection is brutal and our fill model is an optimistic upper bound (no queue position), so the
  honest prior is low. BUT it's the one structurally different bet left (earn vs pay the spread), and at
  megacap spreads with a real qimb adverse-selection filter it's worth one clean test.
- KEEP-AS-LEAD: qimb-maker per-fill bootstrap CI > 0 at the conservative fill model, robust OOS, and BEATS
  the no-overlay baseline (qimb actually helps). Then → a market-making strategy spec (a different container
  class). AMBIGUOUS: positive only under the optimistic fill assumption. KILL: per-fill net ≤ 0 OOS even with
  the qimb filter (adverse selection eats the spread — the standard result for naive retail market-making).
- **Honest expectation:** most likely KILL — naive liquidity provision on megacaps is dominated by
  professional market-makers with better queue position + latency, and adverse selection eats the spread.
  But the test is cheap, structurally different, and the per-fill bootstrap will give a clean answer.

## Ordering

HF03 is cycle 2's third probe and likely its last in the qimb/microstructure family (HF01 taker-directional
KILL, HF02 low-turnover taker KILL, HF03 maker). If HF03 also KILLs, the honest cycle-2 conclusion: the qimb
microstructure signal is real but neither takeable (too faint vs spread) nor makeable (adverse selection) at
our latency/queue position — and the HF-liquid edge, if any, needs either faster infra (genuine
queue-position/latency advantage, which we don't have) or a DIFFERENT liquid signal class (back to the
menu: #2 fundamentals content / #3 ETF flow — Ben's data-acquisition call).
