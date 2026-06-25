# Results — Implied-vol vs the trailing-vol PROXY (closes #331's load-bearing assumption)

- **Code SHA**: `a5dcb3e` (origin/main). NO fingerprint/feature/registry/live edit. Research scratch only.
- **Data**: trailing/forecast vols from store raw 1-min RTH bars as-of **2026-06-18** (latest); real Alpaca
  option-chain **IV snapshot 2026-06-21** (current — no history). Top-N-by-ADV liquid head.
- **Panel**: top-150-by-ADV → **142 names** with both clean bars + valid ATM IV (re-confirmed on an 84-name
  top-90 cut, byte-consistent). ONE row/name. Single cross-sectional snapshot (see honesty caveat §4 of
  PRE_REGISTRATION — this is a G0 screen, NOT a forward backtest).

## Headline: H0 CONFIRMED — the market efficiently prices the same (actually a LONGER) vol term-structure our forecast uses. #331's "vol is efficiently priced into the premium" is now a MEASUREMENT against real IV, not an assumption baked into a trailing-vol proxy. The vol-forecast lane SHELVES.

### T1 — proxy adequacy
- rank-IC(trail30m, ATM-IV) = **+0.74**, OLS R² 0.41, slope +0.52. ATM IV median 0.46 vs trailing-RV
  median 0.42 (IV/trail ratio **1.10–1.17**) — IV sits ~10–17% above trailing realized, the expected VRP.
- Trailing vol explains most but not all of the IV cross-section. The residual is what T2 interrogates.

### T2 — does our forecast predict the IV residual? (the decision test)
- raw rank-IC(forecast60m, iv_resid) = **+0.36..+0.48**; INCREMENTAL (both residualized on trail) =
  **+0.56..+0.57**, boot-95% CI [+0.44, +0.68], shuffle ≈ 0, sign-stable. A *large* incremental IC.
- **BUT the diagnostic shows this is efficient pricing, not a mispricing** (the decisive disambiguation):
  - rank-IC(trail30m, IV) = +0.79 **<** rank-IC(forecast60m, IV) = **+0.86** → IV tracks the **longer
    (60m) term-structure BETTER than the 30m proxy**. So "forecast predicts the IV residual" only because
    IV *already embeds* the longer-window vol the forecast is built from; residualizing IV on the 30m proxy
    alone necessarily leaves a residual the 60m term trivially explains. The market is MORE sophisticated
    than the 30m proxy #331's straddle G0 used, not less.
  - (trail30m and forecast60m are 0.98-correlated, so the joint-OLS individual coefficients are collinear
    and not interpretable; the univariate ICs above are the robust read.)

### T3 — robustness (all sign-stable, magnitude-stable)
- 2nd expiry bucket (dte~47d): incr-IC +0.56–0.58. call-only ATM IV +0.58–0.60, put-only +0.52–0.57.
  Not carried by one wing or one expiry.

### The tradeable check (rules out a real implied-vs-forecast spread)
- IV − forecast(60m) is **positive in every forecast tercile** (median +0.13 overall; IV/forecast ratio
  **1.20 on high-forecast names, 1.33 on low-forecast names**). There is NO name where IV under-prices the
  forecastable vol — the premium is rich relative to the forecast across the board (the structural VRP).
- Critically, the cushion is **thinner** where the forecast is high (1.20×) than where it is low (1.33×) —
  the OPPOSITE of a buyer's edge, and it means conditioning a vol-seller on the forecast harvests LESS, not
  more. This independently reproduces #331's straddle finding (forecast-selection makes the seller worse)
  on REAL IV instead of the proxy.

## Honest verdict
- **H0 confirmed, lane shelves.** The load-bearing assumption behind #331 — "vol is efficiently priced into
  the premium" — holds against real option IV: implied vol tracks the vol term-structure (better than the
  30m proxy) and prices a rich premium over it on every name. Our forecast adds no implied-vs-forecast edge;
  where it differs from the proxy it points the WRONG way for a seller and the premium is rich for a buyer.
- This UPGRADES #331 from a proxy-based conclusion to a measured one. The vol-forecast tradeable lane is now
  comprehensively closed on BOTH the proxy straddle (#331) and real IV (here).
- **The one surviving thread is unchanged and is NOT a forecast play**: the *unconditional* structural
  short-vol VRP (IV ~1.1–1.3× realized) is real but thin, tail-risky, and needs ZERO forecasting. Monetizing
  it remains a premium-harvest play gated on a historical option-IV backfill + tail management + real option
  round-trip cost — a Ben/DI infra decision, not a Modeller edge. This screen does NOT strengthen the case
  for that backfill (the VRP it would harvest is unconditional; our forecast doesn't improve it).

## Methodology notes / caveats (stated, not hidden)
- **Single snapshot**, IV (06-21) post-dates the bars (06-18, last store date) by ~1 trading session +
  weekend. For a cross-sectional *level/structure* comparison across names this is acceptable for a G0
  screen; it is NOT a point-in-time forward study (which needs a historical IV backfill, the same dependency
  #331 flagged). The verdict (efficient pricing) is robust to this lag because both vol regimes are slow-
  moving and the finding is a cross-sectional RANK relationship, not a level-timing claim.
- The pre-registered decision gate's `shuffle-clean` threshold (`|shuffle|<0.03`) mechanically returned the
  H0 label, but the SUBSTANTIVE H0 verdict does NOT rest on that threshold — it rests on the diagnostic
  (IV tracks the longer term better than the proxy + IV rich on every tercile + forecast-cushion inverted).
  A +0.57 incremental IC that means "IV already prices what the forecast knows" is efficient-pricing
  evidence, not a re-open. Recorded transparently so the gate isn't read as the reason.
- `--rm` fp-dev, `/store` RO, single-threaded snapshot read; live capture untouched (no fc/strategy/crypto
  contact). Host load ~16 throughout; the option pulls are network-bound and light.
