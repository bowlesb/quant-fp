# Data-Quality Ledger — the audit loop's standing state

The dedicated **audit loop** (`ops/audit_loop.sh`, host cron) drives this file: each cycle it audits the
next groups that are not yet `OK`, fixes LOCAL per-group issues, escalates SYSTEMIC ones to
`AUTONOMOUS_BACKLOG.md` P1.0, and updates the status here. **Stop condition: every active group is `OK` or
`BLOCKED-ON-SYSTEMIC` (with the systemic fix tracked in the backlog).** When all are resolved, the loop
idles with a one-line "all groups OK" report until new data/regressions appear.

Status legend: `OK` (audited, clean or fixed+verified) · `FIXED-CODE` (code fix landed + parity-tested,
data recollection pending the clean restart) · `ISSUES-LOCAL` (open per-group fix, do it here) ·
`BLOCKED-ON-SYSTEMIC` (waiting on an engine/store fix owned by the main loop) · `UNAUDITED` ·
`PENDING-STREAM` (needs trades/quotes streaming first).

## ⚠️ SEQUENCING RULE
Do NOT restart/redeploy capture to ship feature-code fixes until **CRITICAL-2 (restart warm-start)** is
fixed — a restart currently wipes the live buffers and re-corrupts long-window features. BATCH all feature
code fixes, land the warm-start fix, then do ONE clean restart + recollect contaminated data from backfill.

## Systemic root causes (owned by the MAIN loop via backlog P1.0 — audit loop ESCALATES, does not fix)
| id | issue | status |
|----|-------|--------|
| CRITICAL-1 | `points()` lag features 100% NaN on live — ALL assemble paths (incl. deployed batch `assemble_from_long`) evaluated points on a 1-minute frame | FIXED-CODE+MERGED (`fix/points-lag-live-parity` → integration/converged `c419519`: central `resolve_points` over whole buffer + parity-test null-mask fix + regression/gap tests). Recompute lag-point data after the clean restart |
| CRITICAL-2 | capture restart wipes ring/incremental buffers → long-window collapse; no warm-start from store. **Wave-3 evidence:** also manifests WITHOUT a restart — the trailing buffer (`DEFAULT_BUFFER_MINUTES=300`, capture.py:45) is shorter than a 390m session, so stateful/cumulative groups lose pre-buffer history: swing `n_pivots_today` DECREASES intraday + `minutes_since_pivot` pins at 299; price_levels `dist_from_high_120m==240m` for 100% of late-session rows post-open re-seed; prior_day anchor NaN ~88min for 26% of names. | IN-PROGRESS (`feat/warm-start-ring` → integration/converged `ceb43e8`: `warm_start_ring` rehydrates the ring from settled session bars, parity-true, behind `FP_WARM_START` default OFF; map+reduce paths wired, 6 parity tests). **STILL OPEN:** the buffer-depth/session-anchoring decision (300m<390m needs a profile before bumping); then flip the flag at the clean restart + recompute. See backlog P1.0 CRITICAL-2. |
| CRITICAL-3 | `breadth` computed PER-SHARD (8 distinct values/min) — `REDUCE_GROUPS` omits it; all breadth wrong + parity break | OPEN (backlog P1.0) |
| HIGH-SECTOR | `sector_map` table EMPTY (FMP ingestion never wired) → sector group 100% `unknown` + sector_breadth dead | OPEN (backlog P1.0) |
| HIGH-DAILYLOAD | **(NEW wave-4)** daily-broadcast family (`multi_day_returns`, `multi_day_vwap`, `prior_day`) 100% NULL from the open until **~10:58 ET** every session — the settled `daily` frame isn't loaded into the feature-computer until mid-morning (`consolidated.py _merged_daily` left-joins a not-yet-populated frame). Verified on fully-warmed AAPL (null until 11:05 ET). Multi-day signal is dark through the open. Invisible to tail-sampled audits. | OPEN (backlog P1.0; prod/ingest lane — load daily frame at session start + fail-loud assert) |
| MED-BETA | `market_beta_*` out-of-range ±7700 from degenerate 2-point OLS fits (shared kernel guard too loose) | OPEN (backlog P1.0) |
| P0-UNIVERSE | **(escalated wave-4)** STREAM feature universe = the contaminated 11,336-member / ~34-39%-fund set (`symbols=11336`; membership 39.6% fund-like). Confirmed LIVE across 4 universe-pinned groups: cross_sectional_rank (38.6% funds — leveraged ETFs top the deciles), multi_day_vwap (24.6%), multi_day_returns (27.6%), residual_analysis (SOXL/TQQQ/AAPD/AAPU present). 2026-06-11 ETF-pollution lesson recurring, worse. Same as QA loop's P0 `etf-contamination`. | OPEN (backlog P1.0; prod/universe lane — re-apply `is_etf_like` filter; recollect after) |
| MED-XSPIN | **(NEW wave-4)** `cross_sectional_rank` per-minute universe pin is dead code (`cross_sectional_rank.py:73,101` gate on `ctx.frames["universe"]`, never populated by `capture.py:263`) → ranks over "whoever printed" (nsym 63→4,809/min), a parity hazard even with a clean universe | OPEN (backlog P1.0; capture must supply the universe frame — group code ready) |
| MED-DEDUP | duplicate (symbol,minute) rows (broadcast symbols written by all 8 shards) | OPEN |

## Per-group status
| group | status | open issue / note | audited |
|-------|--------|-------------------|---------|
| volume | FIXED-CODE | std=0 zscore guard (e0c9957); recollect after clean restart | wave1 |
| technical | ISSUES-LOCAL | bb_position inf guarded (e0c9957); STILL: enforce warmup on long SMAs (`sma_dist_{50,100,200}m` compute partial-window ≈0 instead of NaN) | wave1 |
| volatility | BLOCKED-ON-SYSTEMIC | CRITICAL-2 (realized_vol collapse); Parkinson OK | wave1 |
| momentum | BLOCKED-ON-SYSTEMIC | CRITICAL-2 (long-horizon collapse from 14:58 UTC) | wave1 |
| ohlc_vol | BLOCKED-ON-SYSTEMIC | CRITICAL-2 (60/120m collapse) | wave1 |
| return_dynamics | FIXED-CODE | CRITICAL-1 fixed (ret_accel_* now resolve via resolve_points); autocorr OK; recompute after restart | wave1 |
| price_returns | OK | clean | wave1 |
| price_volume | OK | clean (short-window corr degenerate = expected) | wave1 |
| distribution | OK | clean (power-sum safe on returns) | wave1 |
| candlestick | OK | clean | wave1 |
| efficiency | FIXED-CODE | CRITICAL-1 fixed: all 18 feats now resolve live (resolve_points over whole buffer == backfill, parity-tested). NB no efficiency backfill exists yet ⇒ recompute needs a backfill run | wave2 |
| momentum_consistency | FIXED-CODE | CRITICAL-1 fixed: `consistent_direction_*` (6) now resolve live. Local note: `momentum_acceleration` valid_range (-50,50) slightly tight (max 74 on penny names) — cosmetic, not enforced | wave2 |
| trend_quality | FIXED-CODE | flat-price R²→0 guard (so trend_strength=0 not null), parity-safe null↔NaN, landed this cycle. Residual: CRITICAL-2 long-window collapse (r2_90m==120m==180m 100% post-restart) | wave2 |
| clean_momentum | OK | value-side clean (all in [0,1], 0 Inf). Residuals are SYSTEMIC: warmup not gated per-window (long scores from 1-2 pts) + ETF contamination in stream universe (CRITICAL-2 / universe) | wave2 |
| breadth | BLOCKED-ON-SYSTEMIC | CRITICAL-3: computed per-shard (8 values/min); + sector_breadth dead via HIGH-SECTOR; + ETF pollution. All 30 cols corrupt | wave2 |
| sector | BLOCKED-ON-SYSTEMIC | HIGH-SECTOR: 100% `unknown` (sector_map empty). Group code correct; needs FMP ingestion, no bar recollect | wave2 |
| market_beta | BLOCKED-ON-SYSTEMIC | MED-BETA: ±7700 out-of-range from 2-pt OLS fits + warrants in universe. SPY self-beta=1.0 (join healthy). Shared-kernel fix | wave2 |
| round_levels | OK | math correct, 0 NaN/Inf, ranges hold. Flags: fixed-$ grid is price-scale-incomparable (modeller design note) + ETF/non-equity universe (systemic) | wave2 |
| asset_flags | OK | 4 static flags {0,1}, 0 nulls, correct; only value-safe MED-DEDUP | wave3 |
| calendar | OK | ET-clock EXACT (0/645k mismatch), parity by construction; dtype declared-vs-stored note | wave3 |
| calendar_events | OK | pure timestamp math, opex/quarter-end correct; no dead upstream | wave3 |
| cross_sectional_rank | BLOCKED-ON-SYSTEMIC | P0-UNIVERSE (38.6% funds in ranked set) + MED-XSPIN (dead universe-pin → ranks over whoever printed). Group MATH clean: 0 Inf, all in [0,1], no dups, correct warmup. Recollect after universe re-clean | wave4 |
| market_context | OK | SPY/QQQ broadcast exact, 1 value/min; 120m null until buffer warms | wave3 |
| momentum_run | FIXED-CODE | residual_skew relative-floor (84bbb7d); recompute on restart | wave3 |
| multi_day_returns | BLOCKED-ON-SYSTEMIC | HIGH-DAILYLOAD (null until 10:58 ET) + P0-UNIVERSE (27.6% funds). Group PIT math clean (no look-ahead, correct warmup, 0 Inf). Local notes: declared Float64 stored Float32 (cosmetic), valid_range tight for thin names. Recollect after fixes | wave4 |
| multi_day_vwap | BLOCKED-ON-SYSTEMIC | P0-UNIVERSE (24.6% funds) + HIGH-DAILYLOAD. Group logic OK (QA loop audit). Local: valid_range (-1,5) tight for thin microcaps (cosmetic) | wave4 |
| price_levels | BLOCKED-ON-SYSTEMIC | CRITICAL-2 120m/240m collapse post-open re-seed; algebra correct | wave3 |
| prior_day | BLOCKED-ON-SYSTEMIC | HIGH-DAILYLOAD anchor NaN until ~10:58 ET for 26% of names; math clean | wave3 |
| residual_analysis | FIXED-CODE | dropped 12 dead-constant features (mean_abs≡0, symmetric≡1) — landed+parity-tested this cycle. residual_std clean (0 Inf, 0 OOR, no blow-up — guard works). Residual: P0-UNIVERSE (funds inherited). Recompute on restart | wave4 |
| swing | BLOCKED-ON-SYSTEMIC | CRITICAL-2 buffer<session collapses zigzag (n_pivots_today decreases intraday, minutes_since_pivot pins at 299); + queued LOCAL fib clamp (`fib_retracement`→3101 vs ±10, `rust/src/lib.rs:860-861`) batched with the dev-image rebuild + restart | wave3 |
| trade_flow | PENDING-STREAM | needs trades streaming | - |
| quote_spread | PENDING-STREAM | needs quotes streaming | - |
| tick_runlength | PENDING-STREAM | needs trades streaming | - |
| microstructure_burst | PENDING-STREAM | needs trades/quotes | - |
| liquidity | PENDING-STREAM | needs trades/quotes (roll-spread autocovariance) | - |
