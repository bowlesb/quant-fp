# Results — vol/magnitude-predictability edge: scope + G0 net-of-cost screen

- **Code SHA**: `7120c1d` (origin/main). No fingerprint/feature/registry change. Bounded `--rm`/`-d`
  fp-dev, `/store` RO, `USE_REALIZED_COST=0`.
- **Panel**: 2026-05-29..06-18 (14 dates), top-200 by ADV, >=09:35 ET 30-min cadence, $1 + $50k floors,
  **30,292 entries × 200 symbols**. Built clean from contiguous 1-min raw bars. Headline horizon H=30
  forward 1-min bars (robustness at H=15 below).

## Headline: the vol signal is REAL and INCREMENTAL-over-persistence (a genuinely positive, different-than-direction result) — but it does NOT monetize as a forecast-driven edge net-of-cost. The G0 is a forecast-edge NULL with a structural-VRP side-finding.

### 1. The signal — incremental-over-persistence is CONFIRMED (the crux)

Trailing-vol → forward-RV persistence is enormous (raw IC **+0.894**, NW t 154) — vol clusters, as
expected. The question is whether anything adds content BEYOND it. After rank-residualizing both the
signal and forward-RV on the trailing-vol baseline:

| feature | raw IC vs fwd-RV | INCREMENTAL IC (over trailing vol) | incr NW t | collapse |
|---|---|---|---|---|
| realized_vol_60m | +0.892 | **+0.243** | **+10.6** | 0.273 |
| spread_bps_15m | +0.564 | **+0.081** | **+8.1** | 0.144 |
| realized_vol_5m | +0.765 | +0.080 | +6.6 | 0.105 |
| realized_vol_15m | +0.868 | +0.062 | +4.6 | 0.071 |
| trade_freq_15m | +0.210 | +0.047 | +3.9 | 0.224 |
| book_depth_1m | +0.107 | +0.043 | +5.5 | **0.405** |
| peak_trades_per_second_1m | +0.166 | +0.035 | +3.2 | 0.212 |
| max_runup_1m | +0.305 | +0.026 | +3.3 | 0.086 |
| ret_5m / ret_15m / ret_30m | ~0 | ~0 | <1 | — |
| realized_vol_30m | +0.888 | −0.169 | −10.4 | (IS the baseline; self-cancels) |

- YES, there is genuine incremental predictive content beyond "vol is persistent". It is dominated by
  the **vol term-structure** (a 60-min window forecasts the next 30-min RV beyond the 30-min trailing
  window: incr IC +0.243, t +10.6), but includes **non-vol microstructure** — spread (+0.081, t +8.1),
  book-depth (+0.043, t +5.5, highest collapse=0.405 ⇒ most independent of persistence), trade-frequency
  and burst. Shuffle IC ≈ 0 throughout (no leakage).
- DIRECTION features (ret_*, signed_volume, imbalance) add nothing — "magnitude yes, direction no",
  consistent with the standing meta-conclusion. So Ben's reframe is right: the magnitude predictability
  is genuine and is NOT the look-ahead grading artifact.

### 2. Tradeable expression — Alpaca OPTIONS ARE accessible (a key enabler)

Verified with our existing keys: `alpaca-py 0.43.4` `OptionHistoricalDataClient` →
`OptionChainRequest("SPY")` returns **13,349 contracts WITH implied_volatility (e.g. 0.259) + greeks**.
So a straddle expression AND a variance-risk-premium (predicted-RV vs option-implied) trade are both
feasible with no new market access. CAVEAT: option snapshots are current-only; a VRP / option-P&L
*backtest* needs a historical option-quote/IV backfill we do not yet have. The G0 below therefore
screens the straddle with a persistence-vol premium proxy (no option backfill required).

### 3. G0 net-of-cost $-screen (straddle, H=30) — the binding constraint

Per-entry straddle net in bps of underlying, premium ≈ 0.8·trailing_rv·sqrt(H), round-trip swept; MEDIAN
is the tradeability gate (a positive mean on a fat right tail is not tradeable).

**Vol BUYER (long straddle on predicted-HIGH-vol names): loses everywhere.**

| selection | rt2% median | rt10% median | win-rate |
|---|---|---|---|
| BUY_ALL (no selection) | −8.4 | −11.1 | 0.38 |
| realized_vol_60m top-10% | −35.9 | −46.0 | 0.36 |
| spread_bps_15m top-10% | −21.8 (mean +5.4) | −28.1 | 0.36 |

Selecting predicted-high-vol names makes the buyer's median net WORSE (those names carry richly-priced
premiums — premium scales with vol — so realized rarely beats premium). A few cells have a positive MEAN
(spread top-5 +24.9 at rt2) but a sharply negative median + 33-37% win-rate = the fat-tail trap, NOT
tradeable.

**Vol SELLER (short straddle): a small positive median = the STRUCTURAL VRP, which our forecast does NOT improve.**

| selection | rt2% median | rt10% median | rt15% median | win-rate(rt2) |
|---|---|---|---|---|
| **SELL_ALL (no selection)** | **+7.1** | +4.4 | +2.7 | 0.60 |
| SELL spread_bps_15m bot-10% | +3.7 | +2.2 | +1.3 | 0.59 |
| SELL realized_vol_60m bot-10% | +2.2 | +0.9 | +0.2 | 0.56 |
| SELL trailing_rv bot-10% | +1.3 | +0.2 | −0.4 | 0.55 |

- A naive vol-seller (SELL_ALL) collects a small positive median (+7.1 bps at rt2, decaying to +2.7 at
  rt15) — the well-known structural variance-risk-premium (realized < priced). It needs **zero
  forecasting**.
- **Conditioning on our forecast makes it WORSE, not better**: every predictor-selected bottom-cut is
  BELOW SELL_ALL (selection concentrates into low-vol = low-premium names, shrinking the harvest). So
  our vol forecast adds no $-value to the seller.
- The seller's MEAN is negative beyond rt2 (the short-gamma tail: occasional large realized moves), and
  the median harvest after a realistic 5-10% options round-trip is +2 to +4 bps — thin, and the G0's
  premium proxy understates the real short-option gamma/tail risk.

## Honest verdict

- **The vol-predictability signal is genuine and incremental-over-persistence** — a real, positive,
  DIFFERENT result from the direction nulls. The vol lane is no longer untested: features (vol
  term-structure + spread/depth/flow) DO forecast forward realized-vol beyond pure persistence (NW t up
  to +10.6, shuffle-clean).
- **It does NOT clear net-of-cost as a forecast-driven tradeable edge** in the straddle expression. The
  vol-buyer loses (median −20..−66 bps); the vol-seller's small positive median is the UNCONDITIONAL
  structural VRP, which our forecast does not improve (selection makes it worse). So the G0 $-screen is a
  **NULL for the forecast edge** — but a different, more-informative null than direction: we actually ran
  the vol lane, and the binding obstacle is that vol is so persistent that it is (efficiently) priced
  into the premium, leaving no forecast-driven $ after cost.
- **Side-finding worth recording**: the structural short-vol premium (SELL_ALL +2..+7 bps median,
  win-rate 60%) is real but thin and tail-risky; monetizing it is a *premium-harvest* play (not our
  forecast), and would need a real option-quote backfill + tail management before it's a candidate.

## Robustness (H=15, `results_h15.json` / `g0_straddle_h15.json`)

Re-run at a 15-bar forward+trailing window CONFIRMS both halves: the incremental-over-persistence signal
is even stronger (realized_vol_60m incr IC +0.399 t +22.7, realized_vol_30m +0.334 t +21.9, spread_bps
+0.112 t +11.3), and the G0 verdict is identical — vol-buyer loses (BUY_ALL median −5.4, vol_60m top-10%
−21.6), vol-seller's small positive median is unconditional (SELL_ALL +4.4) and forecast-selection only
shrinks it (SELL bottom-cuts all below SELL_ALL). Not horizon-specific.

## Battery friction / infra notes

1. **Battery intraday multi-group join FANS OUT**: `build_intraday_panel` over >=3 feature groups across
   several dates produced 3.06M rows with one symbol_code carrying 1.5M rows over 33 distinct minutes (a
   cartesian explosion), vs a clean ~13 rows/symbol/day. The look-ahead sweep (#326) panel was inflated
   by this. Single-group / few-date builds are clean. This experiment sidesteps it by building from raw
   bars + per-group deduped joins. **Flagged for a battery fix** (likely a non-unique join key or the
   forward-excess self-join; the per-group join should `.unique(subset=["symbol","minute"])`).
2. A historical **option-quote/IV backfill** is the missing dependency for a true VRP/option-P&L test
   (snapshots are current-only). Alpaca exposes `OptionBarsRequest`/`OptionTradesRequest` historically,
   so the backfill is feasible if the vol lane is pursued further.
