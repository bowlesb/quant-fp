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

A naive maker backtest that assumes "I always get filled at the touch and earn the full spread" is fantasy.
The three things that make or break it, all pre-committed:
1. **FILL MODELING:** you only get filled when the market trades THROUGH your posted price. Approximate from
   the trades tape: a passive bid at the prevailing best-bid is filled when a trade prints at/below it (a
   sell hitting the bid). This is a CONSERVATIVE proxy (ignores queue position — you may NOT be first in
   queue), so flag fills as an UPPER bound on fill rate.
2. **ADVERSE SELECTION (the killer):** you get filled on your bid precisely when sellers are aggressive =
   often right before the price drops. So the realized P&L of a filled passive bid = +half-spread − (the
   subsequent adverse move). The NET of (spread earned − adverse move) is the whole question. Measure the
   post-fill mid move over the holding window and subtract it.
3. **INVENTORY/EXIT:** a filled bid leaves you LONG; you exit by posting an ask (earn spread again) or
   crossing (pay spread). Model both; the realistic case is a mix. No infinite inventory — cap it.

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
