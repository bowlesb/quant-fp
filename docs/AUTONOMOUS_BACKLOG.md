# Autonomous Backlog — the loop's goal-driven queue

The autonomous loop reads THIS file every cycle, maintains the live system, then advances the
single highest-priority unchecked improvement item. It is the operational expression of our goals
(parity-true real-time features × ~10k equities, sub-minute bet latency, validated trust, edge hunt).

**Loop discipline each cycle:** MAINTAIN (healthcheck + safe-fix) → ADVANCE (top unchecked item) →
RECORD (commit + progress note) → REFILL (if the improvement queue is thin, append new high-value
items so the loop NEVER idles). Mark items `[x]` when done, with the commit/branch in the line.

---

## P0 — Maintenance (every cycle, recurring)
- [ ] Run `docker exec feature-computer python -m quantlib.ops.healthcheck`; safe-fix dead/wedged containers and empty universe only; escalate anything else at the TOP of the note.
- [ ] Confirm collection: capture Up, SIP ESTABLISHED, freshness < 3 min, coverage growing, no crash loops.

## P1.0 — DATA QUALITY & PARITY (do FIRST — corrupt/missing feature data invalidates all downstream modeling)
Source: wave-1 data-quality audit, 2026-06-15 (10 groups). Findings are HEAVILY duplicative — fix the systemic root causes once. Full evidence: `docs/progress/2026-06-15-audit-wave1.md`. Deleting/recollecting today's data is explicitly authorized (pre-alpha).
- [ ] **CRITICAL — `points()` lag features emit 100% NaN on the live stream (parity break).** `incremental.py` evaluates `points()` exprs on a single-minute frame, so `close.shift(k>0)` is null → `ret_accel_*` (return_dynamics), `consistent_direction_*` (momentum_consistency), `efficiency_ratio_*`/`directional_efficiency_*` (efficiency) = ~29 dead features. Backfill computes them fine ⇒ live≠backfill. FIX (central, `incremental.py`): route `points()` lag exprs through the lag-resolution/slice path (slice deep enough for the max point lag, e.g. 120m — `DERIVE_SLICE=6` is far too shallow for points), evaluate against the lag-resolved buffer not the latest-minute frame. Add a live-vs-backfill parity assertion exercising a points-with-shift group. Also fixes `trade_flow` once trades stream.
- [ ] **CRITICAL — capture restart wipes the live ring/incremental buffers → long-window features (60m/120m...) collapse + emit post-warmup NaN for the rest of the session.** Confirmed in volatility/momentum/ohlc_vol; the two resets today (15:00 & 15:11 UTC) were deploy restarts. No warm-start from the store. FIX: on capture startup, rehydrate each shard's `MinuteRing`/incremental state from the last `reduce_buffer_minutes()` (~120m) of the store (or a persisted snapshot) so a restart no longer erases trailing history. REQUIRED for the nightly relaunch (#3) too. After the fix, **delete & recompute today's contaminated long-window data via the backfill `compute()` path** (immune to the reset).
- [ ] **MED — duplicate (symbol,minute) rows (up to 8×, every group).** Index/broadcast symbols (SPY/QQQ/IWM + ETFs) are written by all 8 shards; byte-identical. Benign IF consumers dedup, but fix at the write/compaction layer (single-writer for broadcast symbols, or dedup-on-write keep-last). Until then, all readers MUST `.unique(subset=["symbol","minute"])`.
- [x] **std=0 guards** — `volume_zscore_*` (0/0 NaN) and `bb_position_20m` (±inf) now guarded; parity-safe. (e0c9957)
- [ ] **Continue the group audit in waves of ~10** (read-only auditors → orchestrator dedups & fixes). DONE wave-1 (clean: distribution, candlestick, price_returns, price_volume; fixed/flagged: volume, technical, volatility, momentum, ohlc_vol, return_dynamics). REMAINING (~20): asset_flags, breadth, calendar, calendar_events, clean_momentum, cross_sectional_rank, efficiency, market_beta, market_context, momentum_consistency, momentum_run, multi_day_returns, multi_day_vwap, price_levels, prior_day, residual_analysis, round_levels, sector, swing, trend_quality. (efficiency + momentum_consistency already implicated in the points() bug above.) Record each group's verdict in the audit doc; fix duplicative roots once.

## P1 — Improvement (ordered; do the top unchecked one)
- [ ] **Stream trades + quotes** (currently bars-only: `real_capture` calls only `subscribe_bars`; `trade_flow`/`quote_spread`/`tick_runlength`/`microstructure_burst`/`liquidity` produce 0 data). Add `subscribe_trades`/`subscribe_quotes` + tick aggregation into those groups. Parity must hold. (Separate from the bar-feature work; enables 5 dormant groups.)
- [ ] **Per-symbol fast/tick path on the REAL feed** — the only route to sub-minute bet latency. Unify the real-feed entrypoint with the fast/tick path so a symbol's vector is emitted as its bar arrives, not after the minute closes. Measure before/after on the bar→vector Grafana dashboard. PARITY MUST HOLD (live == backfill).
  - **2026-06-15 increment (513ee8c / merge 59e3f77):** removed the fixed-symbol-set blocker — `IncrementalEngine` now handles a FLUCTUATING active set (live delivers ~4k of 11.3k symbols/minute, churning). Absent symbols fold a 0 contribution (== a missing bar in the batch) and are masked out of the OLS pairing; a genuinely-new ticker raises `SymbolSetExpanded` → `step()` re-seeds (parity-safe resync). Proven cell-for-cell vs `rust_windowed_sums` and vs batch `compute_latest` under churn (`tests/test_fp_incremental_membership.py`). Library-only, inert on live path, no deploy.
  - **NEXT STEP (next cycle):** wire `IncrementalEngine` into `capture.process_bars` behind `FP_INCREMENTAL` (default OFF) for the batched `ReductionGroup`s — hold one engine per `reduce_input` bucket in `CaptureState`, seed from the universe at session start, `step_rust_unified` each minute instead of `compute_reduction_batch`. Add a runtime parity self-check (`FP_INCREMENTAL_PARITY`) comparing incremental vs batch each minute → a metric. Keep default OFF so no deploy risk; flip only after the self-check runs clean live. THEN tackle emit-on-arrival (the actual latency win) + the open slice-derive constraint below.
  - **OPEN PARITY CONSTRAINT (must resolve before the fast SLICE path goes live):** `DERIVE_SLICE=6` min assumes active symbols are dense; a present symbol whose prior bar is >6 min back would slice-derive a wrong short-lag value (this cycle validated membership with the gap-safe whole-buffer derive, `slice_derive=False`). Options for next owner/cycle: per-symbol variable slice to reach the last bar, recompute the rare sparse symbols whole-buffer, or null/skip a symbol with a stale derive (don't bet on it). DO NOT ship the slice fast-path live until this is parity-gated.
- [ ] **Parity validation ledger live** — run the after-market cycle so `feature_validation_day` / `feature_trust` populate; certify per-feature trust grades; gate training on certified features. (Crown jewel — empty today.)
- [ ] **Nightly re-seed + relaunch automation** — the capture is launched with a HARDCODED `2026-06-15` date arg; it will NOT roll to tomorrow. Wire a nightly job: re-seed universe for the new session + relaunch capture with the new date. Required for true autonomous daily operation.
- [ ] **Prometheus stale-rule cleanup** — `ingestor_alerts.yml` still references the retired `ingestor-coverage` job (now no series). Prune or repoint.
- [ ] **Latency drill-down hardening** — once the fast path lands, re-key the `bar_to_vector_latency` thresholds to the new architecture; confirm per-ticker drill-down still meaningful.

## P2 — Breadth & features (do after P1 is clear, or interleave when P1 is blocked)
- [ ] Port remaining high-value features from the old codebase (continue the FEATURE_PORT_PLAN).
- [ ] Real ADV-dollar ranking to replace the alphabetical/placeholder universe ordering once backfill history accrues.
- [ ] EDGAR real-time ingestion feature (filings event-clock).

## P3 — Research (non-blocking, when compute is idle)
- [ ] DL foundation-model prototyping on the clean panel (3090).

---

## How to REFILL (never idle)
If every P1/P2 item is checked or blocked, append new items drawn from: gaps the healthcheck WARNs at,
new failure modes seen in `docs/progress/`, feature ideas, parity edge cases, latency reductions, and
the Definition-of-Done criteria. Half the job is innovation — generate, don't stall.

## Log
- 2026-06-15: universe 3,070→11,336 (bias removed); dual latency metric + per-ticker drill-down + Grafana; continuous healthcheck engine + 5-min tripwire + MAINTENANCE_PROTOCOL.md. (integration/converged)
- 2026-06-15 ~11:40 ET: P1 #1 increment — `IncrementalEngine` fluctuating-symbol-set support (the live-integration blocker), parity-proven under membership churn. (513ee8c → merge 59e3f77, integration/converged)
