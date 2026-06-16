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
Anchored at the **ACTUAL fill timestamp** — the first trade-through inside the resting window — NOT the
window edge. `fill_mid` = the prevailing quote mid at that fill instant (the honest reference; `post_mid`
is up to 10s stale by the time the fill prints). After each fill we MARK OUT at the prevailing **mid** at
**fill_ts + {1s, 5s, 30s, 60s}**.
- Bid fill => we are **long** at post_bid. `markout_net(s) = (mid_at_s - post_bid)/post_mid`
  = `earned_spread (fill_mid - post_bid)` + `adverse_drift (mid_at_s - fill_mid)`. Reported in bps of mid.
- Ask fill => we are **short** at post_ask, symmetric.
- **cross exit** variant: also pay the half-spread to flatten at the horizon (pessimistic exit bound).
- **adverse-selection magnitude** = the `adverse_drift` term: how far the mid moved against the fill over
  the hold (the cost the earned spread must overcome).

**Note on anchoring.** An earlier pass anchored the mark-out at the resting-window OPEN (post_ts+10s) and
referenced the stale post_mid, which over-counted the earned spread (~1.05 bps). The corrected fill-time
anchoring measures the spread we actually captured at the fill instant (~0.87 bps trade_through / 0.65 bps
queue_proxy) and the post-fill drift over the TRUE hold. The corrected numbers are more conservative and do
not change the verdict.

**HEADLINE = the per-FILL mark-out-net mean with a per-fill 95% CI.** At n_fills ≈ 0.5–1.5M per cell the
CI half-width is set by the standard error (≈ 1.96·sd/√n) and is razor-thin, so the reported analytic CI is
identical in conclusion to a per-fill bootstrap (the early window-edge run used a 2000-resample
memory-bounded bootstrap and gave the same intervals). KEEP only if that CI excludes zero **above** at the
conservative trade-through fill, OOS, AND the qimb overlay beats the no-overlay baseline.

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
