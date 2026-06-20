# Lane D EDGE HUNT — VERDICT (EDGAR + sector signal surface)

**Date:** 2026-06-19 · **Pre-reg:** `prereg.md` (written before any outcome) · **Panel:** 117,555 rows,
60 sampled trading days 2018→2025, 1,116 symbols, top-300-liquid/day, tradeable entries ≥09:35 ET,
`available_at + 5min` embargo on every filing. Stats: `screen.py` → `screen_results.csv`.

## TL;DR — an HONEST, decisive result (the turbulence lesson, confirmed and sharpened)

1. **DIRECTION is a CLEAN NULL.** 0 of the 13 direction pairs (EDGAR recency/burst → forward signed
   return; sector_excess reversal/momentum) survive BY-FDR. Best direction pair = `sector_excess_15 →
   y_ret_30m` at shuffle-z **1.87** (dies at q=0.10). **The 3 settled price-surface direction-nulls extend
   to the EDGAR and sector-relative surfaces** — neither EDGAR filing activity nor within-sector excess
   predicts intraday cross-sectional DIRECTION here.

2. **MAGNITUDE is dominated by VOLATILITY PERSISTENCE — NOT net-new.** 13 magnitude pairs pass BY-FDR on
   raw shuffle-z (z up to 20), but **10 of the 13 COLLAPSE to ~0 under the own-vol/mkt_rv marginal control**
   (collapse ratio < 0.13 for the move-magnitude/RV targets). E.g. `mins_since_8k → y_fwd_rv`: raw z=12.9,
   partial r=0.0003, **collapse 0.005**. The eye-popping "recent 8-K → higher forward realized vol" is
   almost entirely the names that were ALREADY volatile (vol persistence the shipped per-symbol vol
   features capture). Reporting it as an EDGAR edge would be exactly the fooling the marginal control exists
   to catch.

3. **ONE genuinely NET-NEW signal survives the own-vol control: EDGAR activity → forward VOLUME.** Same
   signature as Modeller3's turbulence→volume finding:

   | feature | target | raw shuffle-z | partial r (own-vol control) | collapse | OOS | BY-FDR |
   |---|---|---|---|---|---|---|
   | `mins_since_8k` | forward log-volume | 20.1 | **−0.066** | **0.76** | consistent | ✅ |
   | `edgar_burst_7v90` | forward log-volume | 7.6 | **−0.038** | **1.37 (strengthens)** | consistent | ✅ |
   | `edgar_cnt_7d` | forward log-volume | 4.9 | **−0.025** | **1.31 (strengthens)** | consistent | ✅ |

   Sign is economically sensible: a more RECENT 8-K (smaller `mins_since_8k`) and a higher recent filing
   burst/count → HIGHER forward volume, NET of own-vol persistence (collapse ≥ 0.76, two of three
   STRENGTHEN under the control = independent of vol persistence), OOS-sign-consistent across the
   2018-2021 / 2022-2025 day-split. This is an **information-arrival → liquidity/participation** effect.

## Is it TRADEABLE? — honest framing (NO over-claim)

**No direct return edge.** The surviving signal predicts forward **VOLUME**, not forward return — there is
no long/short return spread to cost, so the prereg's cost-net decile step is **N/A by construction** (a
volume predictor has no signed P&L). It is the same class of result as turbulence→volume: a **vol/intensity
predictor, not alpha.** Its value is (a) an **execution/sizing conditioner** (anticipate the liquidity
surge after a filing burst → better fills / capacity), and (b) a **battery faithfulness target** — a
strategy archetype run on this surface should reproduce: direction null, magnitude≈vol-persistence,
volume-predictability net-new. It is NOT a standalone tradeable strategy and is NOT proposed for promotion.

## Disposition

- **No promotion, no feature PR, no excitement to escalate.** The net-new piece (EDGAR→volume) duplicates
  the already-established turbulence→volume axis; it adds an EDGAR *driver* of that same volume signal, not
  a new tradeable direction. I am NOT flagging a confirmatory replication because nothing here is a
  return edge that would warrant one.
- **The decisive negative is the deliverable:** the EDGAR + sector surface, screened with full discipline
  on a 60-day/8-year panel, is **directionally NULL** and its magnitude content is **vol persistence**, with
  a single net-new VOLUME (not alpha) effect. This settles whether this newly-built surface is directionally
  tradeable: it is not, on the intraday cross-sectional framing. Combined with the 3 prior direction-nulls,
  the next hunt should change the FRAMING (longer horizon, event-study around the filing instant itself
  rather than a cross-sectional minute panel, or a volume/liquidity-target strategy family that USES the
  one thing that does predict — intensity) rather than mine more cross-sectional direction features.

## Method notes / caveats (for the reader + the adversarial auditor)

- `available_at` is the SEC "submissions_accepted" instant (look-ahead-safe) + a conservative 5-min
  embargo; 2016+ intraday timing is real (8-Ks cluster post-16:00-ET-close), not a midnight artifact.
- H1 (an event-intensity claim, not purely cross-sectional) used timestamp FIXED EFFECTS (de-mean the
  target within each (day,minute) block) so a market-wide move cannot masquerade as a filing effect; H2
  (cross-sectional) used within-block rank-IC with a Newey-West t over the per-block IC series.
- Marginal control partials out `own_rv_30` + the universe `mkt_rv_30` from BOTH sides; the collapse ratio
  = |partial|/|raw| is the headline anti-fooling number.
- 60 days sampled across 8 years is a SCREEN, not a full backtest — the OOS split is a day-split, not a
  walk-forward retrain. A survivor here would warrant a deeper disjoint-year replication; nothing survived
  that needs it.
