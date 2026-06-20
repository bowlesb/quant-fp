# PRE-REGISTRATION — Stage 2: PREDICTED per-name cost model for live/forward use

**Date:** 2026-06-20  **Author:** Modeller  **Status:** GATE-READ REQUESTED (no code built)
**Relation to Stage 1:** Stage 1 (PR #271, merged) wired the REALIZED per-name half-spread, measured from
the quote tape, into the harness BACKTEST cost — backtests are now honest (median 8.39 vs flat 3.0 bps).
Stage 2 is the PREDICTED model for FORWARD/LIVE decisions, where realized cost is unknown at the entry
instant. **KEY DISTINCTION held throughout: Stage 1 = measured cost for backtests (truth, shipped); Stage 2
= predicted cost for live (model). Do not conflate; do not productionize prediction where measurement
suffices.**

---

## 0. ⚠️ PRIORITY / CRITICAL-PATH CHECK (read this first — it changes the urgency)

**The live executor charges NO synthetic cost stub today, and trades NO real capital.** Verified in the code:
- The live/paper path is `PaperAlpacaExecutor` — `TradingClient(..., paper=True)`, "it never touches a live
  account." The 3 running strategies (overnight-beta, smoke, reversion) are PAPER, filling at REAL Alpaca
  paper prices (actual fills) — there is no synthetic cost term to replace in the live path.
- The flat-stub cost (`MarketSnapshot.half_spread_bps`, `DEFAULT_HALF_SPREAD_BPS`) lives ONLY in the
  `FaithfulBacktestExecutor` (the SIM) and the harness — i.e. the BACKTEST, which Stage 1 already fixed.
- The live pre-trade gate `pre_trade_check` (G4) is a buying-power / shortable / PDT check — **there is no
  cost gate** today.

**Conclusion: Stage 2 is NOT on the critical path right now.** Its live value (cost-aware sizing, a pre-trade
cost gate, forward $-projections) only matters when REAL capital trades — which (per Ben's profile) is gated
on paper proving out first. Stage 1 already delivered the urgent win (honest backtests). **Recommendation:
PRE-REGISTER Stage 2 now (cheap — captures the design while it's fresh) but SEQUENCE THE BUILD behind a fresh
edge hunt** (see the companion NEXT-EDGE note). Build Stage 2 when real-capital execution is imminent, or
earlier only if an edge is found that needs cost-aware live sizing to be tradeable.

The rest of this doc is the committed design, so the build is a known quantity whenever it's sequenced.

---

## 1. What Stage 2 is (and is NOT)
- **IS:** a per-name forward half-spread/impact PREDICTOR (the G0b model: a GBM on point-in-time quote
  proxies, validated OOS R²=0.575 / rank-IC=0.902 / 59% MAE-cut vs the flat stub) that estimates the cost a
  name WILL cost to trade over the next holding window, for use at the decision instant when realized cost is
  not yet observable.
- **Used by (forward/live only):** (a) the live executor's pre-trade cost gate (reject/with-reason a leg
  whose predicted cost exceeds a per-strategy budget), (b) cost-aware sizing (down-weight high-predicted-cost
  names), (c) forward $-projections (what a strategy is expected to net AFTER cost, before it trades).
- **Is NOT** used for backtests (Stage 1's measured realized cost is ground truth there — always prefer
  measurement to prediction when the data is available ex-post).
- **Is NOT** alpha. It is execution infrastructure; it improves net $ by avoiding cost, not by predicting return.

---

## 2. Construction (point-in-time, G-STALE, parity-safe)
- **Features:** the G0b quote proxies computed strictly point-in-time at the decision instant T from the
  trailing quote window `[T-W, T)` — time-weighted spread mean/std/trend, top-of-book imbalance mean/trend,
  log depth + depth vol, quote intensity, **quote staleness** (T − last NBBO change). G-STALE enforced: reads
  ONLY quotes `ts < T` (strict µs); a stale-quote name is flagged and routed to the conservative fallback
  (§4), never predicted as if fresh.
- **Target:** the realized forward time-weighted half-spread over the holding window — the SAME quantity
  Stage 1 measures (`quantlib.data.realized_cost`), so training labels = Stage-1 ground truth (no new label
  pipeline; the truth source and the prediction target are the same function).
- **Model:** GBM (the validated G0b choice). Monotonic constraints where sensible (predicted cost
  non-decreasing in trailing spread/staleness) to keep it well-behaved out-of-distribution.

## 3. Training / storage / versioning
- **Train** offline, walk-forward, on the quote-tape window; FROZEN into a versioned artifact
  `cost_model/v<X.Y.Z>/model.txt` + a `meta.json` (train window, feature list, OOS R²/MAE/IC, the
  conservative-fallback bound). Same frozen-artifact pattern as the harness `RankModel`.
- **Versioned & pinned:** the live executor loads a pinned cost-model version (like a feature fingerprint);
  a new model is a new version, never an in-place swap. The artifact records its training regime so drift
  (§5) is detectable.
- **Refresh cadence:** re-fit on a rolling window on a schedule (cost regime drifts with volatility); each
  refresh re-runs the §5 validation before it can be pinned.

## 4. Parity-safe portability (live == the validated model)
- The quote proxies are **columnar** (rolling spread/imbalance/depth aggregations + an asof-backward join to
  T) → implement in **polars**, parity-by-construction: the live bus quote stream and the backfill tape run
  the SAME expression. The staleness/intensity statistics, if per-quote-stateful, follow the swing_dc pattern
  (a pinned Rust kernel + a Python reference oracle); PREFER columnar.
- **Feeds `long_short_per_name_cost` live + the executor:** the predicted per-name half-spread becomes the
  `half_spread_bps` the live `MarketSnapshot` / cost accounting uses (today unset in the live path) and the
  number the pre-trade cost gate checks against the per-strategy budget. A cost-INPUT change, not a
  decide-core change (same as Stage 1) — fingerprint-neutral.

## 5. ⭐ VALIDATION-AGAINST-TRUTH + degradation alarm (Stage 1 gives us ground truth — use it)
Because Stage 1 measures realized cost, the predictor can be scored against truth CONTINUOUSLY, not just at
train time. PRE-COMMITTED:
- **Continuous OOS scoring:** every traded (or candidate) name×instant, log predicted cost; T+1, measure the
  realized cost via Stage 1 and score predicted-vs-realized R² / MAE / rank-IC, **bucketed by regime** (VIX
  band, time-of-day, liquidity decile) — so we know WHERE the model is trustworthy, not just on average.
- **Degradation alarm (committed thresholds):** if rolling predicted-vs-realized **R² drops below 0.40** OR
  **MAE rises above 1.5x its validation MAE** (rolling 5-day, per regime), RAISE an alarm AND **fall back to a
  CONSERVATIVE BOUND** — the max(predicted, a regime-conditioned high percentile of recent realized cost) —
  so a degraded model can only ever OVER-charge (safe: it suppresses trades), never under-charge (unsafe:
  lets a costly trade through). Never silently trust a drifted model.
- **Acceptance to pin a version:** OOS R² ≥ 0.50 and MAE ≤ 0.6x the flat-stub MAE on the held-out window,
  across all regime buckets (not just pooled) — else it does not replace the prior pinned version.

## 6. Gates (build only after a GO on §0 sequencing)
| # | Gate | Bar |
|---|------|-----|
| S2-1 | Point-in-time / G-STALE | predictor reads only quotes ts<T; stale→fallback; bit-identical truncate-at-T test |
| S2-2 | Validation-against-truth | continuous predicted-vs-realized R²/MAE/IC by regime (§5), acceptance bar met on held-out window |
| S2-3 | Degradation alarm + conservative fallback | the §5 alarm + over-charging fallback wired and unit-tested |
| S2-4 | Parity-safe | live quote-proxy compute == backfill (columnar polars / pinned kernel) |
| S2-5 | Wiring | feeds `long_short_per_name_cost` live + the pre-trade cost gate; fingerprint-neutral; cost-INPUT only |
| S2-6 | No-real-capital guard | ships behind the same paper-only posture; the live cost gate is exercised on paper before any real-capital use |

**Decision rule:** PRE-REG approved → hold the build until (a) real-capital execution is sequenced, or (b) a
found edge needs cost-aware live sizing. When built: S2-1..S2-6 all pass before the live executor pins it.

---

## 7. Honest scope note
Stage 2 is correct and cheap to pre-register, but building it now would be productionizing prediction where
(today) measurement suffices and no real capital is at risk. The higher-value use of the same hours is a
fresh edge hunt — see `NEXT_EDGE_NOTE.md`. I recommend the Lead sequence that ahead of the Stage 2 build.
