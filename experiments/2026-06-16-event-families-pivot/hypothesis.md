# Event-family pivot (H4 splits / H5 dividends / H10 EDGAR 8-K drift) — pre-registration + DATA NEEDS

**Registered:** 2026-06-16 (before any run). This is the PIVOT TARGET if H9 (longer-horizon vwap_dev
reversion) KILLS — i.e. if vwap_dev is dead at all tradeable horizons (15→120 min) and under all
conditioners (H1–H3). The standing position then: NO price-only OR order-flow CROSS-SECTIONAL signal clears
the cost gate. The unexplored class is **LOW-TURNOVER, EVENT-DRIVEN, NON-PRICE** signals — which attack the
cost wall by DESIGN (a multi-day holding horizon makes the fixed ~6 bps round-trip a tiny fraction of the
move), the opposite end of the lever from the minute-rebalanced price signals that all failed.

## Why this class is the right pivot (the meta-lesson so far)

H1–H3 closed the microstructure-conditioning branch with a durable insight: **conditioning lowers cost but
can't manufacture signal.** H9 tests the last price lever (horizon). If H9 kills, the remaining hypothesis is
that the edge is not in PRICE at all at our latency — it is in EVENTS the market re-prices over days
(corporate actions, filings, fundamentals), where (a) the signal is a real information shock not a
microstructure wiggle, and (b) the multi-day horizon makes cost a non-binding ~1–2% of the move.

## The three event sub-hypotheses (ranked)

### H10 — EDGAR 8-K / Form-4 event drift (HIGHEST priority — the data is now LIVE)
- **Idea:** Material 8-K filings (item-coded: 1.01 material agreement, 2.02 earnings, 5.02 exec change…)
  and Form-4 insider BUYS produce multi-day drift. Build days-since-filing + item-type cohorts with a
  look-ahead-safe `available_at` (the filing's accepted timestamp, never the period-of-report).
- **Prior:** Post-earnings/announcement drift + insider-buy drift are among the most robust documented
  anomalies; orthogonal to intraday price; low turnover.
- **Test:** event-cohort forward returns (1/3/5/10-day) vs matched non-event controls; net-of-cost on the
  multi-day horizon (cost trivial). Same shuffle canary + survivorship caveat.
- **KILL:** event cohorts show no forward-return separation vs controls beyond canary.

### H4 — Corporate-action splits (reverse = distress, forward = attention)
- **Idea:** reverse-split underperformance / forward-split announcement drift; days-to/since-split + is-recent
  flags (δ-delayed, parity-safe).
- **Prior:** documented (Desai-Jain; retail attention post-split). Likely THIN sample (the old repo had ~61
  split events) — may be UNDERPOWERED; if so, document as "needs more history," not "dead."

### H5 — Dividend-timing (the ONLY prior family that survived survivorship demean)
- **Idea:** dividend run-up / ex-date drift; days-to/since-ex, runup window, is-payer.
- **Prior:** surv-neutral breakeven 2.1 bps survived before; re-confirm standalone on a clean panel with the
  canary isolated.

## DATA NEEDS (the ask to route to the backfill agent — flagged to the Lead)

Current `/store/raw` has bars/trades/quotes but **NO corporate_actions and NO filings panel**. For this
pivot I need, point-in-time (each row carries a look-ahead-safe `available_at`/`ex_date`/`filing_accepted_ts`):
1. **EDGAR filings panel** (H10): the LIVE collector is accruing 8-K/Form-4 with `available_at` — I need it
   exposed as a queryable (symbol, filing_type, item_codes, accepted_ts) table or a `/store/raw/filings`
   parquet I can read in the sandbox, covering the same ~6-month window as bars. **Highest-value ask.**
2. **Corporate actions** (H4/H5): a (symbol, action_type ∈ {split, dividend}, ex_date, ratio/amount,
   declared_ts) table. Alpaca has a corporate-actions endpoint; the old repo had a `corporate_actions` table
   to port. ~6-month + ideally deeper for split sample size.
3. Survivorship note: splits/distress especially need delisted names eventually (H8) — acceptable to start
   survivorship-imperfect and document it honestly.

## Ordering / gate

This pivot ACTIVATES only if H9 kills. If H9 KEEPS (a longer-horizon vwap_dev cell clears net-of-cost), that
becomes the lead and this pivot waits. Either way, pre-positioning the data ask now means zero idle time at
the handoff. Within the pivot: **H10 (EDGAR, data live) → H5 (dividends, prior survivor) → H4 (splits,
likely underpowered).**
