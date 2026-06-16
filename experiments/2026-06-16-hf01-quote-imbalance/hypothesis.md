# HF01 — Quote-imbalance / signed-flow predicting next-1-to-5-min megacap return (CYCLE 2, pre-registration)

**Registered:** 2026-06-16, BEFORE any run (cycle-1 discipline). Cycle 2 opens on the HF-liquid intraday
regime — the one thing cycle 1 never tested — authorized as zero-dependency (all data + bus + 2 live
containers in hand; no acquisition, no spend). This is a RESEARCH hunt in `experiments/`; NO feature-group or
fingerprint changes (a survivor becomes a feature LATER via the coordinated deploy).

## Why this regime (the capacity-flip from cycle 1)

Cycle 1 mapped 0/7 tradeable edges; the unifying constraint was that every canary-clearing signal was
illiquid-concentrated and thus untradeable. In LIQUID megacaps that trap vanishes: a $10K order is
0.015–0.06% of ONE MINUTE's flow (measured), capacity is unbounded for $100K, and the half-spread is
0.4–3 bps. **The binding constraint flips from capacity to TURNOVER-COMPOUNDED COST:** at a 1–5 min horizon
turnover is high, so even a ~1 bps round-trip compounds fast. Turnover (and bid-ask bounce), NOT capacity, is
the adversary. The open question: does a real-time microstructure signal exist in megacaps at a horizon
SHORTER than the minute/hour cross-section cycle 1 found efficiently priced?

## Hypothesis

Top-of-book QUOTE IMBALANCE and signed TRADE-FLOW imbalance, measured over a trailing short window (10–60s
to 1–2 min), predict the next-1-to-5-minute MID-price return of a liquid megacap, with a sign consistent with
price-pressure/continuation (positive imbalance → positive next-return), strongly enough to clear a
turnover-compounded cost gate at the MEASURED ~0.5–1 bps half-spread.

Concretely, three candidate signals (all point-in-time, no future leakage), tested standalone + marginal:
- `qimb` = mean top-of-book size imbalance (bid_size − ask_size)/(bid_size + ask_size) over the trailing
  window (the `quote_spread` group's quantity, but at sub-minute resolution).
- `ofi` = Cont–Kukanov–Stoikov order-flow imbalance over the window (signed change in the book — the H2
  quantity, but here at the SHORT horizon where it may live, not the daily cross-section where it died).
- `stflow` = tick-rule signed trade volume over the window, volume-normalized.

## The bid-ask-bounce trap (pre-committed defense — short horizons fool you here)

A naive "next return" measured on the LAST TRADE price is dominated by bid-ask bounce (a sell-then-buy looks
like a +spread move that isn't tradeable). Defenses, pre-committed:
1. **Predict the MID-price return, never the trade-price return.** Mid = (bid+ask)/2 from quotes. This
   removes the mechanical bounce.
2. **Entry/exit at the FAR-TOUCH (you cross the spread): enter buys at the ask, sells at the bid**, and book
   the return mid-to-mid but CHARGE the full measured spread on entry AND exit in the cost gate. A signal that
   only "works" because it's measured edge-to-edge is bounce, not alpha.
3. **Lag the signal by one quote/observation** so the signal at decision time t uses only data < t (no
   same-instant leak).

## Pre-committed GATES (all of cycle 1's, plus the turnover gate)

1. **Shuffle canary FIRST** — permute the forward return within each minute's cross-section / within time
   blocks; 10 seeds; the signal IC must clear the canary band. KILL if inside the band.
2. **Per-symbol demean** — subtract each symbol's own mean forward return (survivorship/idiosyncratic).
3. **Walk-forward OOS** — first ~half of days TRAIN, last ~half OOS; demean within split; anything post-hoc
   (a horizon/window chosen after looking) requires the OOS hold-out to replicate (RESEARCH_PITFALLS #4).
4. **TURNOVER-COMPOUNDED COST GATE (the real adversary):** net = gross − (round-trips per period × measured
   round-trip cost). Round-trip cost = full measured spread (~1 bps) + a tiny impact. At a 1–5 min rebalance,
   a continuously-flipping signal pays cost every period — report turnover and the net AFTER it, and sweep a
   no-trade band / signal-persistence threshold to cut turnover. KILL if no (window×horizon×band) cell nets
   positive after turnover-compounded cost.
5. **Look-ahead guard** — mid-return strictly forward; signal strictly trailing; verified by re-deriving one
   cell by hand.

## Expected / confidence

- Confidence a (signal × window × horizon × band) cell clears canary + demean + OOS AND nets positive after
  turnover-compounded cost: **~30%.** Higher than cycle-1 priors because cost/capacity are finally favorable
  and the SHORT horizon is genuinely untested — but megacaps are the most efficient names and the bounce/
  turnover traps are exactly where HF backtests fool themselves. I pre-commit to that prior.
- KEEP-AS-LEAD: a cell clears all gates incl. turnover-compounded net positive OOS, robust to the 2×-cost
  stress. Then → a parity-safe short-window microstructure feature proposal (LATER, coordinated deploy) + an
  HF paper strategy container.
- AMBIGUOUS: clears canary+demean but net ≈ 0 after turnover, or only pre-band.
- KILL: inside canary, OR net ≤ 0 after turnover-compounded cost in every cell, OR collapses under demean/OOS.

## Universe / power (honest)

The deep-quote megacap set: MSFT (63 quote-days), AVGO (57), AMD (56), TSLA (39), AAPL (35), then
NVDA/SPY/AMZN/META/GOOGL/QQQ/NFLX (~21–25). Quote density ~2,400 quotes/min (abundant for sub-minute). First
hypothesis scopes to the ~8–12 names with ≥21 quote-days; power is honest-but-modest on ~21-day names — if
quote depth is the binding constraint, report it rather than waiting for the 63-day resumption. Bars (mid
proxy via close acceptable as a cross-check) span 126 days but the QUOTE-derived mid is the clean target.

## Ordering

HF01 is cycle 2's first hypothesis. If it KILLs, the next HF probes are: HF02 (sub-minute micro-momentum /
trade-run continuation), HF03 (liquidity-provision: does posting at the touch + capturing spread when the
book is balanced pay, i.e. a market-making rather than directional bet). Each pre-registered before running.
