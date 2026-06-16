# HF03 — Method (passive maker spread-capture, MODELED backtest)

**This is a MODELED backtest with stated, conservative fill assumptions, NOT a live-fill measurement.**
Alpaca paper cannot report queue position or true passive fills, so even a live paper run would not
validate the fill model. Every fill below is an *optimistic upper bound* (see caveat).

## Data
- Universe: HF01/HF02 deep-quote megacaps with >=21 quote-days: MSFT, AAPL, TSLA, AVGO, AMD, NVDA, SPY,
  META, QQQ (AMZN/GOOGL/NFLX dropped at 20 valid days < 21). 9 names, 63 trading dates.
- Raw quotes (`/store/raw/quotes`) and trades (`/store/raw/trades`), genuine UTC.
  RTH = UTC minutes [810, 1190) = 13:30–19:50 UTC = 09:30–15:50 ET (RESEARCH_PITFALLS #1).
- Quote filter: positive bid/ask/size, ask >= bid.

## Posting policy
- Post a passive **bid at the best bid** and a symmetric **ask at the best ask** on a **10s grid** (one
  post per 10s bucket), using the *last* quote in the bucket as the resting book state. Require ask>bid.
- Trailing **qimb** at post time = mean of per-tick (bid_size-ask_size)/(bid_size+ask_size) over a 120s
  trailing window, strict left-closed (no peeking at the current tick).

## Fill model (the first way maker backtests lie — pre-committed against)
A passive bid at price P is **filled only by a TRADE-THROUGH**: a trade prints **strictly below P** within
the next 10s resting window (the market traded through our level, clearing the queue ahead). Symmetric for
the ask (trade prints strictly above the ask).
- **`trade_through`** (primary): any strictly-through print fills us.
- **`queue_proxy`** (stricter): cumulative printed size *through* the level within the window must exceed
  the `bid_size` (resp. `ask_size`) resting at post time — a crude queue-depth proxy.
- **Caveat (flagged):** BOTH are *optimistic upper bounds*. We have no true queue position; a real passive
  order can sit unfilled behind a deep queue even when the level trades through. "Touched my level =>
  filled" is FORBIDDEN and not used.

## Post-fill MARK-OUT (the decisive metric — second way maker backtests lie)
After each fill we MARK OUT the resulting position at the prevailing **mid** at **+1s/+5s/+30s/+60s**
(mid = last quote at-or-before the mark-out instant; mark-out anchored at the resting-window open, a
slightly conservative — longer — adverse-selection exposure).
- Bid fill => we are **long** at post_bid. `net_passive(s) = (mid_at_s - post_bid)/post_mid`. Equivalently
  this already nets the earned half-spread (post_mid - post_bid) against the adverse mid drift
  (mid_at_s - post_mid). Reported in bps of mid.
- Ask fill => we are **short** at post_ask, symmetric.
- **cross exit** variant: also pay the half-spread to flatten at the horizon (pessimistic exit bound).
- **adverse-selection magnitude** = mean (mid_at_s - fill_price) in bps: how far the mid moved against the
  fill on average (the cost the earned spread must overcome).

**HEADLINE = the per-FILL mark-out-net distribution with a per-fill bootstrap 95% CI (10k resamples,
memory-bounded exact resampling).** KEEP only if that CI excludes zero **above** at the conservative
trade-through fill, OOS, AND the qimb overlay beats the no-overlay baseline.

## qimb overlay
- No-overlay **baseline**: post both sides always.
- **qimb overlay**: post the bid only if `qimb >= -thr` (skip when book strongly bid-light => about to
  drop); post the ask only if `qimb <= +thr` (skip when strongly bid-heavy => about to rise). Sweep
  `thr in {0.0, 0.05, 0.10, 0.20, 0.40}`.

## Gates
1. **Decisive:** per-fill mark-out-net bootstrap 95% CI must EXCLUDE zero above (trade-through fill).
2. **OOS:** date-ordered 50/50 train/OOS split; the CI gate must hold on the OOS half.
3. **Canary:** shuffle qimb within (symbol, date) at fill time; the real overlay's mark-out-net must beat
   the shuffled (random-posting) 97.5th percentile — i.e. qimb must actually help, not just sub-select fills.

## Verdict rule
- **KEEP-AS-LEAD:** OOS trade-through qimb-overlay per-fill mark-out-net CI > 0 AND overlay beats baseline.
- **AMBIGUOUS:** positive only under the optimistic fill assumption / only baseline or only overlay passes.
- **KILL:** per-fill mark-out-net <= 0 OOS even with the qimb filter (adverse selection eats the spread).
