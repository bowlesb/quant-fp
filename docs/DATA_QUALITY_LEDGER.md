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
| CRITICAL-1 | `points()` lag features 100% NaN on live (incremental engine evaluates points on a 1-minute frame) | OPEN |
| CRITICAL-2 | capture restart wipes ring/incremental buffers → long-window collapse; no warm-start from store. **Wave-3 evidence:** also manifests WITHOUT a restart — the trailing buffer (`DEFAULT_BUFFER_MINUTES=300`, capture.py:45) is shorter than a 390m session, so stateful/cumulative groups lose pre-buffer history: swing `n_pivots_today` DECREASES intraday + `minutes_since_pivot` pins at 299; price_levels `dist_from_high_120m==240m` for 100% of late-session rows post-open re-seed; prior_day anchor NaN ~88min for 26% of names. Fix needs session-anchored / store-warm-started buffer ≥ longest declared window | OPEN |
| CRITICAL-3 | `breadth` computed PER-SHARD (8 distinct values/min) — `REDUCE_GROUPS` omits it; all breadth wrong + parity break | OPEN (backlog P1.0) |
| HIGH-SECTOR | `sector_map` table EMPTY (FMP ingestion never wired) → sector group 100% `unknown` + sector_breadth dead | OPEN (backlog P1.0) |
| MED-BETA | `market_beta_*` out-of-range ±7700 from degenerate 2-point OLS fits (shared kernel guard too loose) | OPEN (backlog P1.0) |
| MED-UNIVERSE | warrants/`.PR`/`.WS`/`.U`/leveraged-ETFs in the STREAM feature-input universe (ETF-pollution lesson recurring) | OPEN (backlog P1.0) |
| MED-DEDUP | duplicate (symbol,minute) rows (broadcast symbols written by all 8 shards) | OPEN |

## Per-group status
| group | status | open issue / note | audited |
|-------|--------|-------------------|---------|
| volume | FIXED-CODE | std=0 zscore guard (e0c9957); recollect after clean restart | wave1 |
| technical | ISSUES-LOCAL | bb_position inf guarded (e0c9957); STILL: enforce warmup on long SMAs (`sma_dist_{50,100,200}m` compute partial-window ≈0 instead of NaN) | wave1 |
| volatility | BLOCKED-ON-SYSTEMIC | CRITICAL-2 (realized_vol collapse); Parkinson OK | wave1 |
| momentum | BLOCKED-ON-SYSTEMIC | CRITICAL-2 (long-horizon collapse from 14:58 UTC) | wave1 |
| ohlc_vol | BLOCKED-ON-SYSTEMIC | CRITICAL-2 (60/120m collapse) | wave1 |
| return_dynamics | BLOCKED-ON-SYSTEMIC | CRITICAL-1 (ret_accel_* dead); autocorr OK | wave1 |
| price_returns | OK | clean | wave1 |
| price_volume | OK | clean (short-window corr degenerate = expected) | wave1 |
| distribution | OK | clean (power-sum safe on returns) | wave1 |
| candlestick | OK | clean | wave1 |
| efficiency | BLOCKED-ON-SYSTEMIC | CRITICAL-1 CONFIRMED: all 18 feats 100% NaN live (points/shift on 1-min frame); group code correct. NB no efficiency backfill exists yet ⇒ no parity ref / recovery source | wave2 |
| momentum_consistency | BLOCKED-ON-SYSTEMIC | CRITICAL-1 CONFIRMED: `consistent_direction_*` (6) 100% NaN live. Local note: `momentum_acceleration` valid_range (-50,50) slightly tight (max 74 on penny names) — cosmetic, not enforced | wave2 |
| trend_quality | FIXED-CODE | flat-price R²→0 guard (so trend_strength=0 not null), parity-safe null↔NaN, landed this cycle. Residual: CRITICAL-2 long-window collapse (r2_90m==120m==180m 100% post-restart) | wave2 |
| clean_momentum | OK | value-side clean (all in [0,1], 0 Inf). Residuals are SYSTEMIC: warmup not gated per-window (long scores from 1-2 pts) + ETF contamination in stream universe (CRITICAL-2 / universe) | wave2 |
| breadth | BLOCKED-ON-SYSTEMIC | CRITICAL-3: computed per-shard (8 values/min); + sector_breadth dead via HIGH-SECTOR; + ETF pollution. All 30 cols corrupt | wave2 |
| sector | BLOCKED-ON-SYSTEMIC | HIGH-SECTOR: 100% `unknown` (sector_map empty). Group code correct; needs FMP ingestion, no bar recollect | wave2 |
| market_beta | BLOCKED-ON-SYSTEMIC | MED-BETA: ±7700 out-of-range from 2-pt OLS fits + warrants in universe. SPY self-beta=1.0 (join healthy). Shared-kernel fix | wave2 |
| round_levels | OK | math correct, 0 NaN/Inf, ranges hold. Flags: fixed-$ grid is price-scale-incomparable (modeller design note) + ETF/non-equity universe (systemic) | wave2 |
| asset_flags | OK | clean: 4 static tradability flags in {0,1}, 0 nulls, correct on liquid names; only inherited MED-DEDUP (value-identical dups, no corruption) | wave3 |
| calendar | OK | clean: ET-clock EXACT (0/645k mismatch vs recompute from bar-ts), derived from ctx.timestamp not wall-clock ⇒ parity by construction; cross-symbol identical/min. Note: stored dtype UInt16/8 vs declared Float64 (values correct; signed-arith-on-unsigned foot-gun) | wave3 |
| calendar_events | OK | clean: pure timestamp math (no event table to be dead); opex/triple-witching/quarter-end all correct for 2026-06-15; 0 nulls, in-range. Only inherited MED-DEDUP + universe (values unaffected, symbol-independent) | wave3 |
| cross_sectional_rank | UNAUDITED | cross-section gather (480 files, 1/min) | - |
| market_context | OK | clean: SPY/QQQ broadcast EXACT (max diff 0.0 vs price_returns), ONE value/min (no per-shard bug), ranges hold, warmup-null monotone. Note: 120m (and early-session 90m) 100% null until live buffer warms ≥120m — coverage, not a bug | wave3 |
| momentum_run | FIXED-CODE | `residual_skew_{W}m` relative residual-spread floor landed + regression-tested (84bbb7d): was ±1.6e9 vs ±20 from near-linear cancellation noise (0.5% of 5m rows, 184 syms). `longest_streak` clean & in-range. NOT a ReductionGroup ⇒ immune to CRITICAL-2. Recompute residual_skew after clean restart | wave3 |
| multi_day_returns | UNAUDITED | multi-day (warms over days) | - |
| multi_day_vwap | UNAUDITED | | - |
| price_levels | BLOCKED-ON-SYSTEMIC | CRITICAL-2: 120m/240m extrema COLLAPSE onto each other after the regular-session open re-seed (`dist_from_high_120m==240m` 100% of late rows; understates true drawdown on 131/200 liquid names). Group algebra CORRECT; 5/10/15/30/60m trustworthy once warmed. Local cosmetic: `position_in_range` NaN-on-flat-bar mislabeled nan_policy="warmup". Recollect 120m/240m | wave3 |
| prior_day | BLOCKED-ON-SYSTEMIC | CRITICAL-2-class warm-start: prior-day anchor 100% NaN from open until ~10:58 ET for 2,575 names (26%) — daily cache not seeded at live-computer startup (prod-owned). Compute math CLEAN (gap_open sane & constant: AAPL +1.03%, SPY +1.36%). Out-of-range only on warrants/.WS (universe). Recollect pre-10:58 from backfill | wave3 |
| residual_analysis | UNAUDITED | | - |
| swing | BLOCKED-ON-SYSTEMIC | CRITICAL-2 (buffer<session): zigzag state collapses — `n_pivots_today` DECREASES intraday (TSLA 7→6→0), `minutes_since_pivot` pins at 299; live≠backfill late-session ⇒ docstring parity claim false past 300m. Recollect after session-anchored buffer. LOCAL (queued, Rust): `fib_retracement` range blowup max 3101 vs declared ±10 — lib.rs:860-861 loose denominator guard; fix = clamp output to declared range (needs `make dev-image` rebuild — batch with the clean restart) | wave3 |
| trade_flow | PENDING-STREAM | needs trades streaming | - |
| quote_spread | PENDING-STREAM | needs quotes streaming | - |
| tick_runlength | PENDING-STREAM | needs trades streaming | - |
| microstructure_burst | PENDING-STREAM | needs trades/quotes | - |
| liquidity | PENDING-STREAM | needs trades/quotes (roll-spread autocovariance) | - |
