# Feature Taxonomy — the all-encompassing point-in-time ticker vector

The goal (Ben, 2026-06-13): for any ticker at any minute, a vector that captures **the entire context
of that name at that instant** — what it is, where its price sits, how and how much it's moving, who's
trading it, how it relates to the market, and what's around it in time. Broad by design; we add
liberally across horizons and **prune later from data, not from prior assumptions**. The one hard
constraint is unchanged: every feature must compute **identically live and in backfill** and be
covered by the T+1 parity harness (see `PARITY_PLAYBOOK.md`). This doc is the map: the context
dimensions, what's built, and the proposed new categories bucketed by what infra they need.

As of 2026-06-13: **438 features across 22 groups** (`docs/FEATURES.md`).

## Buckets (by parity cost, from the Edgar categorization)
- **A — pure & parity-true now:** a deterministic function of bars / trades / quotes / daily / the
  static reference snapshot. Self-selects into the parity harness; zero new infra.
- **B — needs a new data source + its own parity story:** an external feed (FMP fundamentals, an
  earnings/econ calendar, options) — slowly-changing ones are parity-true *by immutability* if we
  store the snapshot and never recompute it.
- **C — version-pinned derived data:** outputs of a model/parser (news sentiment, embeddings, filing
  NLP). Parity only holds if the model artifact is frozen and the arrival-time is pinned; otherwise a
  backfill re-run silently changes history. Deferred until we have a version-pinning policy.

---

## The context dimensions

### 1. WHAT IT IS — identity / reference  *(partly built)*
- ✅ `sector` (11 GICS one-hots + unknown), `asset_flags` (shortable / easy-to-borrow / marginable /
  fractionable).
- **B — numeric fundamentals:** market cap, float, shares out, short interest / days-to-cover, P/E,
  P/B, size class, 52-week position. Needs an FMP `/profile` + `/quote` source **and per-date
  historization** (market cap moves daily) for true point-in-time. Sector table + fetcher already
  exist (FMP-key-gated); extend the same path.
- **B — borrow dynamics:** borrow fee, utilization, days-to-cover trend (needs a short-data feed).

### 2. WHERE THE PRICE IS — location  *(largely built)*
- ✅ intraday range position, distance from rolling high/low (`price_levels`); MA distances, Bollinger
  position (`technical`); multi-day distance-from-N-day-high, daily levels (`multi_day`).
- **A — session-anchored (propose):** overnight gap, distance from *today's* open / high / low,
  opening-range (09:30–09:35) breakout, session VWAP deviation, time-since-session-high/low.
  **Parity caveat:** these need whole-session retention, which the 300m trailing buffer can't hold —
  build a **per-symbol per-day session-stats snapshot** (same pattern as the daily cache) that the
  live path updates intra-session and the backfill recomputes; broadcast to minutes.
- ✅ **prior-day pivots** (`prior_day`): floor pivots P/R1/R2/S1/S2, overnight gap, distance from
  prior-day high/low/close. The `daily` frame is now wired into the live + parity paths (gap #1
  closed). Round-number proximity still to add.

### 3. HOW IT'S MOVING — direction / momentum / trend  *(largely built)*
- ✅ multi-horizon simple+log returns (`price_returns`), momentum/up-ratio (`momentum`), trend
  slope/R²/strength (`trend_quality`), path efficiency (`efficiency`).
- **A — acceleration / jerk (propose):** momentum-of-momentum, return 2nd/3rd derivative, volume
  acceleration. Pure, trivial via differences of existing windows.
- **A — autocorrelation / mean-reversion signature (propose):** lag-k return autocorrelation via the
  OLS kernel (already built) — distinguishes momentum names from mean-reverters.

### 4. HOW MUCH IT'S MOVING — volatility / risk  *(largely built)*
- ✅ std / realized / Parkinson vol, ATR / range (`volatility`), return skew / excess kurtosis /
  up-down semivariance (`distribution`).
- **A — Garman-Klass / Rogers-Satchell (propose):** OHLC-efficient vol estimators (now that `open`
  is plumbed). Pure.
- **A — vol term structure & regime (propose):** short-vol / long-vol ratio, vol-of-vol; vol
  percentile vs the name's *own* history (needs a daily-vol cache — pairs with the daily frame).

### 5. WHO'S TRADING — flow / microstructure  *(built)*
- ✅ signed volume / trade-rate (`trade_flow`), spread / imbalance / depth (`quote_spread`),
  sub-minute burst / inter-arrival / peak-rate (`microstructure_burst`, Layer C).
- **A/B — liquidity & cost (propose):** Amihud illiquidity (|ret|/$vol), Kyle's λ (impact per signed
  volume), Roll effective-spread, quoted vs effective spread. Layer B (trades+quotes), parity-safe.

### 6. VOLUME CONTEXT  *(built)*
- ✅ volume z-score / ratio / spike / dollar-volume (`volume`); price-volume correlation, OBV slope,
  vwap-deviation, up/down volume, buying pressure (`price_volume`).

### 7. MARKET CONTEXT — relative  *(largely built)*
- ✅ market/nasdaq returns broadcast, relative return, outperformance (`market_context`); rolling
  beta / correlation / idiosyncratic vol vs SPY (`market_beta`).
- **A — cross-sectional rank (propose):** the name's *universe percentile* of return / volume /
  volatility / RSI at each minute — the strongest signal for a ranking model. Cross-sectional, so
  **parity depends on identical universe membership** live vs backfill: pin it with a
  **universe-membership snapshot** per minute/day and rank within it.
- **B — sector-relative (propose):** return minus within-sector-within-minute mean (sector-neutral
  momentum). Needs sector data populated (FMP gate) + the cross-section.

### 8. TIME CONTEXT — calendar / events  *(partly built)*
- ✅ time-of-day, session phase, day-of-week, distance to open/close (`calendar`).
- **A — pure-calendar event proximity (propose):** days to/from month/quarter/year end, options
  expiry (3rd Friday / OPEX), triple-witching, half-days. Deterministic from the timestamp.
- **B — earnings / econ proximity:** days to/since earnings, is-earnings-window, FOMC/CPI proximity.
  Needs an earnings + econ calendar feed (FMP / a macro source); slowly-changing → snapshot-parity.

### 9. NEWS / SENTIMENT — *(bucket C)*
- Article counts, FinBERT sentiment, keyword flags, embeddings. **Deferred:** model outputs must be
  version-pinned or backfill ≠ live; plus headline arrival-time ambiguity.

### 10. FILINGS / INSIDER — *(bucket C)*
- 8-K item flags, Form-4 insider direction, filing context. **Deferred:** parser must be
  version-pinned and `filed_at` arrival-time pinned.

### 11. OPTIONS / DERIVATIVES — *(bucket C/B)*
- IV rank / term structure, put/call ratio, dealer gamma, 25-delta skew. Needs an options feed.

---

## Known wiring gaps to close (parity-relevant)
1. ✅ **CLOSED (2026-06-13) — `daily` frame wired into live + parity.** `backfill_daily` now carries
   full OHLC; `real_capture` loads the daily history once at startup into `snapshots`, and
   `parity_test` feeds the same daily frame to both sources. `multi_day` and the new `prior_day`
   group are now parity-covered and served live. (Validate against real captured-vs-backfilled data
   on the next session.) Still open under this dimension: vol-vs-own-history (needs a daily-vol cache).
2. **Numeric fundamentals need historization.** A single current snapshot is fine for parity-by-
   immutability only if stored per-date; market cap / short interest move, so capture a daily
   reference snapshot (not a live recompute) to keep point-in-time honest.
3. **Cross-sectional features need a pinned universe.** Rank/sector-relative features must rank within
   the *same* symbol set live and in backfill; snapshot universe membership per day and rank within it.

## Near-term build order (all bucket A, zero/again-cheap infra)
1. Acceleration, autocorrelation, Garman-Klass/RS vol, liquidity/cost, pure-calendar event proximity
   — pure, parity-true, large feature count.
2. Wire the `daily` frame into live + parity (gap #1) → pivots, prior-day anchors, vol-vs-history.
3. Cross-sectional rank with a pinned universe snapshot (gap #3).
4. Then bucket B: FMP numeric fundamentals + earnings calendar (snapshot-parity); sector data
   populate (unblock the FMP key) lights up `sector` + sector-relative.
