# Data-Quality Audit — Wave 4 (the LAST 4 groups) — 2026-06-15 ~13:40 ET

**MILESTONE: every active feature group is now audited.** No group is `UNAUDITED` or `ISSUES-LOCAL`.
Tally: **11 OK / 7 FIXED-CODE / 12 BLOCKED-ON-SYSTEMIC / 5 PENDING-STREAM** (full table in
`docs/DATA_QUALITY_LEDGER.md`). The audit loop's local-fix work is **done**; all remaining blockers are
systemic (main-loop-owned) or await trades/quotes streaming.

## FOR THE OWNER (action needed)
- **NEW systemic, money-relevant (escalated to backlog P1.0 `HIGH-DAILYLOAD`):** the entire daily-broadcast
  feature family — `multi_day_returns`, `multi_day_vwap`, `prior_day` — is **100% NULL from the open until
  ~10:58 ET every session**. Proof: fully-warmed AAPL (240+ days history) emits `daily_return_1d=null` until
  11:05 ET; all three groups flip to ~99.7% valued at the IDENTICAL 10:58 ET boundary → the cause is the
  shared `daily` frame not being loaded into the feature-computer until mid-morning (`consolidated.py
  _merged_daily` left-joins a not-yet-populated frame), NOT any group's logic. **Multi-day momentum/
  mean-reversion/VWAP-distance signal is dark through the open — the most tradeable window.** It is invisible
  to tail-sampled audits (the prior multi_day_vwap audit sampled the late session and reported "0.4% NaN").
  Fix = load the settled daily frame at session start + a fail-loud assert. **Prod/ingest lane.**
- **P0 universe contamination is confirmed LIVE across 4 universe-pinned groups** (escalated to backlog
  `P0-UNIVERSE`): the stream feature universe = the 11,336-member / ~34–39%-fund set (`symbols=11336`;
  membership 39.6% fund-like). cross_sectional_rank's ranked set is **38.6% funds** — TQQQ/SOXL/AAPB/AAPD/AAPU
  top the deciles and mechanically push every real equity's percentile down (the 2026-06-11 ETF-pollution
  lesson recurring, worse). multi_day_vwap 24.6%, multi_day_returns 27.6%, residual_analysis SOXL/TQQQ/AAPD
  present. Same root as the QA loop's open P0 `etf-contamination`. **Re-apply the equities-only universe
  filter + recollect.** Prod/universe lane.
- **Two LOCAL fixes landed this cycle (parity-tested, merged to `integration/converged`):** see below. Both
  are metadata/declare-level or schema-level — no live value change, no restart needed for them to take
  effect for consumers (the residual_analysis column-drop recomputes on the next clean restart per the
  SEQUENCING RULE).

## LOCAL fixes landed (my lane)
1. **`residual_analysis` — dropped 12 dead-constant features** (`19972d3` / merge `8486e3b`). 12 of 18
   columns carried ZERO information: `residual_mean_abs_*`≡0.0 (the OLS mean residual is identically zero by
   construction) and `residuals_symmetric_*`≡1 (derived as mean_abs<0.1). Confirmed dead on today's store
   (distinct sets `{0.0}` and `{1}` across 66k–93k rows, all 6 windows). Kept `residual_std_*` (the only
   informative family — values clean: 0 Inf, 0 out-of-range, the `(n>=4)&(sxx_c>0)` guard prevents the
   blow-ups that hit market_beta/momentum_run). A genuine residual-ASYMMETRY feature (signed 3rd moment)
   needs power sums this group doesn't accumulate → left as a modeller note. Parity-safe (one code path).
2. **`technical.sma_dist_*` — nan_policy `warmup`→`sparse`** (`cf1cf08`). The SMA is `_close/_close_n`,
   defined from the first bar, so sma_dist has NO warmup null-prefix — it emits a partial-window value
   (≈0 early). "warmup" implied a null prefix that doesn't exist; "sparse" is honest. Metadata-only → zero
   value/parity/recollect impact. Closes the last `ISSUES-LOCAL` in the ledger. (Gating the early partial
   window to null is a separate modeller signal-design choice — needs an elapsed-minutes signal threaded
   through 5 reduction paths; not a corruption/parity bug.)

## Wave-4 verdicts
| group | verdict | one-liner |
|---|---|---|
| cross_sectional_rank | BLOCKED-ON-SYSTEMIC | math clean (0 Inf, [0,1], no dups); P0-UNIVERSE 38.6% funds + MED-XSPIN dead universe-pin (`:73,101` gate on never-populated `ctx.frames["universe"]` → ranks over whoever printed, nsym 63→4,809/min) |
| multi_day_returns | BLOCKED-ON-SYSTEMIC | PIT math clean (no look-ahead, correct warmup, 0 Inf); HIGH-DAILYLOAD null-until-10:58 + P0-UNIVERSE 27.6% funds; local cosmetics: declared f64/stored f32, tight valid_range on thin names |
| multi_day_vwap | BLOCKED-ON-SYSTEMIC | logic OK (QA-loop audit); P0-UNIVERSE 24.6% funds + HIGH-DAILYLOAD; tight valid_range on thin microcaps (cosmetic) |
| residual_analysis | FIXED-CODE | 12 dead constants dropped (this cycle); residual_std clean; residual P0-UNIVERSE inherited; recompute on restart |

Also synced 4 stale wave-3 rows the prior ledger commit missed (asset_flags/calendar/calendar_events OK,
swing BLOCKED-ON-SYSTEMIC + queued local fib clamp).

## NEXT
The audit reaches its STOP condition: every active group is OK / FIXED-CODE / BLOCKED-ON-SYSTEMIC /
PENDING-STREAM. Subsequent cycles should hit the STOP CHECK → light re-verify of one previously-OK group
(regression guard) until new data/regressions appear. The keystone that unblocks the FIXED-CODE recollects
remains **CRITICAL-2 warm-start** (main loop, in progress). The two systemic findings escalated this cycle
(HIGH-DAILYLOAD, P0-UNIVERSE) are prod/ingest-lane and block trusted use of the cross-sectional + daily
families until fixed + recollected.
