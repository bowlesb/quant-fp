# W11 — Overnight-BETA premium: method

Pre-registered in `hypothesis.md` (read it first). This is **not** W4 (W4 = the plain overnight return
LEVEL, killed as survivorship). W11 tests the **conditional, beta-sorted** split: high-minus-low-beta L/S is
predicted POSITIVE overnight (close→open) and ≤0 intraday (Hendershott–Livdan–Rösch 2020, JFE).

## Data & universe
- Source: `/store/raw/bars` (1-minute bars, hive-partitioned `symbol=/date=`), 126 trading dates
  2025-12-15 → 2026-06-16, ~6,100 symbols with usable RTH days.
- **RTH window** = UTC minute ∈ [810, 959] (13:30–15:59 UTC = 09:30–15:59 ET regular session). Bars carry
  genuine UTC ts (RESEARCH_PITFALLS #1); pre-market bars (UTC min < 810) are excluded. `dt.hour()` was cast
  to Int32 before `*60` to avoid an Int8 overflow that initially wrapped the minute-of-day.
- Per (symbol, date): `rth_open` = open of the FIRST RTH bar (the 09:30 ET print), `rth_close` = close of
  the LAST RTH bar (16:00 ET close), `dollar_vol` = Σ(close·volume) over RTH bars. Days with <30 RTH bars
  dropped (half-days / illiquid stubs).
- **LIQUID universe** = top **500** symbols by **median daily dollar volume**, requiring ≥100 days of
  history (beta needs trailing data). SPY is NOT used as the market proxy: SPY bars are sparse in this store
  (only 63/126 days, not even top-500 liquid), so it would give an unreliable beta. **Market = equal-weight
  liquid-universe 24h return per date** (stated, per the hypothesis' allowed proxy).

## Returns (per symbol, date)
- `overnight = rth_open_t / rth_close_{t-1} − 1`  (close→open; the bet is buy-at-close, sell-at-open)
- `intraday  = rth_close_t / rth_open_t   − 1`
- `ret24     = rth_close_t / rth_close_{t-1} − 1`  (the market proxy is the equal-weight mean of this)
- First day per symbol dropped (no prev close); non-finite dropped.

## Beta estimation
- `beta_i` = OLS slope of symbol i's `ret24` on the market `ret24` over a **trailing 60-day** window that
  **ends strictly before** the estimation date (point-in-time; the estimation date itself is excluded).
- Re-estimated every **21 trading days** (monthly). A symbol needs ≥20 paired obs in the window.

## Portfolio & 3-way realization
- At each rebalance, sort the liquid names with a valid beta into **5 quintiles**; LOW = bottom quintile
  (~100 names), HIGH = top quintile (~100 names). Equal-weight within each leg.
- Hold for the **next 21 trading days** (strictly forward of the beta window → no look-ahead in the held
  returns). Realize the **high-minus-low-beta L/S** THREE ways over the hold:
  (a) OVERNIGHT-only, (b) INTRADAY-only, (c) 24h.
- Per-rebalance observation = the **average daily L/S return over the hold** (the non-overlapping unit for the
  bootstrap). The split (a vs b) is the primary test.

## Cost
- Turnover = fraction of each leg's membership that changes between consecutive rebalances (beta is slow →
  low turnover). Charge one **round-trip** per holding period: `cost = 2 · (spread_per_side) · turnover_frac`,
  spread_per_side = **3 bps** (liquid-name proxy); first rebalance charged full turnover. Reported gross, net,
  and **2× stress**. **MOO/MOC auction caveat**: the overnight bet executes at the close auction (entry) and
  the open auction (exit); the 3 bps proxy is a quote-spread approximation, not the realized auction slippage
  — the deeper-history certification must measure actual auction fills.

## Gates
- **Shuffle-canary**: permute the beta→name mapping before sorting (breaks the beta signal, keeps the leg
  structure) → the overnight L/S should collapse toward zero.
- **Walk-forward OOS**: held returns are already forward of the beta window; additionally split the rebalances
  into first-half (IS) / second-half (OOS) and bootstrap the OOS overnight net.
- **Per-rebalance bootstrap** (10k resamples, 95% CI) on the non-overlapping per-rebalance overnight L/S.
- **Robustness**: winsorize overnight at ±15% and a **median-of-leg** L/S (robust to a few extreme gappers)
  — to check the result is not driven by a handful of speculative-name overnight gaps.

## Honest power caveat
126 days, 60-day beta warmup → only **~3 non-overlapping rebalances**. The bootstrap resamples 3 points →
wide/degenerate CIs; the IS/OOS split has 1 IS + 2 OOS rebalances (not a real OOS). **The SPLIT direction is
the robust signal here; the net-of-cost CI is underpowered.** A ≥18-month bar history is required to certify.
