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
| CRITICAL-2 | capture restart wipes ring/incremental buffers → long-window collapse; no warm-start from store | OPEN |
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
| asset_flags | UNAUDITED | | - |
| calendar | UNAUDITED | | - |
| calendar_events | UNAUDITED | | - |
| cross_sectional_rank | UNAUDITED | cross-section gather (480 files, 1/min) | - |
| market_context | UNAUDITED | | - |
| momentum_run | UNAUDITED | | - |
| multi_day_returns | UNAUDITED | multi-day (warms over days) | - |
| multi_day_vwap | UNAUDITED | | - |
| price_levels | UNAUDITED | min/max windows | - |
| prior_day | UNAUDITED | prior-session anchored | - |
| residual_analysis | UNAUDITED | | - |
| swing | UNAUDITED | Rust zigzag | - |
| trade_flow | PENDING-STREAM | needs trades streaming | - |
| quote_spread | PENDING-STREAM | needs quotes streaming | - |
| tick_runlength | PENDING-STREAM | needs trades streaming | - |
| microstructure_burst | PENDING-STREAM | needs trades/quotes | - |
| liquidity | PENDING-STREAM | needs trades/quotes (roll-spread autocovariance) | - |
