# Vol/magnitude-predictability EDGE — scope + G0 net-of-cost screen (2026-06-21)

Ben's reframe (2026-06-21): the recurring "magnitude/vol-predictability" result is NOT a null — it's a
REAL, ~10x-confirmed, never-traded signal (features predict forward realized-vol/range, IC +0.2..+0.3).
DIRECTION is the settled null; the magnitude half is genuine. The "circularity" flagged in the
look-ahead sweep (#326) was only the battery's grading artifact (a direction-L/S basket booked against a
positive-only label). This experiment finally runs the "predict vol/range, not direction" target-pivot
lane: pin the signal (with the incremental-over-persistence crux), assess the tradeable expression, and
G0 net-of-cost screen it.

## 1. The signal (`vol_edge_screen.py`)

THE CRUX = INCREMENTAL-OVER-PERSISTENCE. Vol is extremely persistent, so a forecast that only restates
"vol clusters" is not an edge — it must beat the naive trailing-vol baseline. For every candidate feature
we report, on a pooled cross-sectional panel:

- raw cross-sectional rank-IC vs forward realized-vol,
- within-timestamp SHUFFLE null (leakage canary),
- ⭐ INCREMENTAL rank-IC after rank-residualizing BOTH the signal and the forward-RV target on the
  trailing-vol baseline (collapse = |incr|/|raw|; ~0 = pure persistence, >~0.3 = net-new content),
- the trailing-vol baseline's OWN IC vs forward-RV (the persistence bar to beat).

PANEL (built clean directly from contiguous 1-min raw bars — the battery's multi-group intraday join
fans out at >=3 groups x several dates, a separate bug flagged below; sidestepped here):
- 2026-05-29..06-18 (14 dates), top-200 by ADV, tradeable >=09:35 ET 30-min cadence, $1 + $50k floors.
- forward realized-vol = std of the next 30 contiguous 1-min log-returns (the proper RV substrate).
- trailing realized-vol = std of the prior 30 contiguous 1-min log-returns (point-in-time, no look-ahead).
- candidate features = trusted store features (volatility / price_returns / microstructure_burst /
  trade_flow / quote_spread) joined per-group at the entry minute.

## 2. The tradeable expression

The signal is a forward-VOL forecast; the natural monetizations are options/vol trades. Assessed:
- **Alpaca OPTIONS data IS accessible** with our existing keys (verified: alpaca-py 0.43.4
  OptionHistoricalDataClient → SPY OptionChainRequest returns 13,349 contracts WITH implied_volatility +
  greeks). So both a straddle expression and a variance-risk-premium (predicted-RV vs implied) trade are
  feasible — but a VRP/option-P&L BACKTEST needs a historical option-quote/IV backfill we do not yet have
  (snapshots are current-only). The G0 therefore screens the straddle expression with a persistence-vol
  premium proxy (no option backfill needed).
- **vol-targeting / range-conditioned equity** is monetizable within existing equity access but is a
  risk-management overlay, not an alpha — out of scope for a $-edge screen.

## 3. G0 net-of-cost $-screen (`g0_straddle.py`)

The standing G0 discipline: a cheap throwaway-proxy net-of-cost screen BEFORE proposing a build; the
binding constraint is net-of-cost $, not IC. Expression = a long (or short) ATM straddle held H bars on
predictor-selected names. Straddle payoff ~ |forward move|; cost = premium (priced off expected vol) +
options round-trip. Per-entry net (bps of underlying):

    BUY:  realized|move| - premium - round_trip      SELL: premium - realized|move| - round_trip

Premium proxy (throwaway): a vol-seller prices off the expected move, so premium ≈ 0.8 * trailing_rv *
sqrt(H). Round-trip swept over {2,5,10,15}% of premium (ATM options on liquid names). MEDIAN (not mean)
is the tradeability gate — a positive mean carried by a fat right tail is not tradeable (#197 lesson). We
report the $-curve per predictor × cut × round-trip, vs BUY_ALL/SELL_ALL + a random-selection control.

## Results

See `RESULTS.md` (the incremental-over-persistence leaderboard + the G0 $-curve + the honest verdict).
Raw: `results_h30.json`, `g0_straddle_h30.json`.
