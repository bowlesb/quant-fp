# Scope memo — full clean-universe historical backfill (task #12)

**Owner:** prod-architect · **Status:** SCOPE ONLY (execution gated on Manager approval after M1
critical path #2+#4) · **Date:** 2026-06-12

## Goal
Make the **research panel == the live tradable universe**, so any edge we validate is computed on
the cross-section we actually trade. M2 exit criterion.

## The gap — measured, not estimated (and SMALLER than first framed)

I earlier flagged "~285 live names have ZERO backfilled history." **That was an overstatement** —
direct measurement against the 2026-06-12 clean universe (1000 names):

| live universe | any backfill | full history (≤2024-01-15) | partial history | zero backfill |
|---|---|---|---|---|
| 1000 | 1000 | 750 | **250** | **0** |

So: **every live name has some history; 750 are complete; 250 are PARTIAL** (first bar after
2024-01-15). The divergence is *depth* for 250 names, not *absence*. The PIT `universe_membership`
already self-corrects: build_universe_history screens from available backfill bars, so a partial
name simply isn't a member on dates before it has data — early cross-sections are ~742, recent
approach ~1000. The panel is more aligned than my first message implied.

### Fixable vs. not
The 250 partial names split into:
- **Genuine post-2024 listings / IPOs** — no earlier data EXISTS; correctly absent from early
  cross-sections. NOT a fixable gap (and not a bias — they truly weren't tradable then).
- **Liquid-earlier-but-unbackfilled** — were tradable before their first bar but we never fetched
  it (they entered our backfill set late). THIS is the fixable slice.
- Exact split = a cheap `asset_metadata` listing-date vs first-bar check (deferred — avoided now to
  not load the DB during the panel rebuild). Estimate: of 250, perhaps 100-150 fixable.

### Note the bigger survivorship point is SEPARATE (task #9)
This memo is about *living* names with short history. The delisted-name survivorship gap (losers
that left the universe) is task #9 — different problem, different source (Alpaca drops dead
tickers). Both matter; this one is cheaper and lower-risk.

## Cost to close the fixable slice

Baseline already on disk: **1213 symbols, 253.5M backfill rows, 11 GB (compressed), 2023-12→2026-06**
(~209k rows & ~523 bar-days per full-history symbol; ~43 bytes/row compressed).

Backfilling ~150 fixable names' missing early portion (worst case = full 2.5yr each):
- **Rows:** ≤ 150 × 209k ≈ 31M rows (realistically ~half, since they have partial data) → **~15-31M rows**
- **Disk:** ~15-31M × 43 B ≈ **0.7-1.3 GB** (negligible; 2.6 TB free)
- **API:** Alpaca bars, ~1 paginated request per symbol-month (21d×390min ≈ 8.2k bars/page). ≤150
  names × ~30 months = **~4500 requests**; at the ~200 req/min Algo-Trader-Plus ceiling ≈ **~25 min**
  of API time (less, since only missing months) + processing.
- **Wall-time:** order of **30-90 min** including DB upserts, comparable to one panel-rebuild pass.

## Recommended approach — incremental via existing infra (no new tooling)

The `backfill-manager` service ALREADY self-maintains history to `BACKFILL_TARGET_DAYS` for universe
symbols (idempotent month-window walk, rate-limited, resumable via `backfill_windows`). Two-step:

1. **One-shot gap fill** (supervised): run `backfiller backfill-bars BACKFILL_SYMBOLS=<the fixable
   names> BACKFILL_START=2024-01-01` — same path as the panel backfill, bounded to the gap names.
2. **Standing fix:** raise `BACKFILL_TARGET_DAYS` toward ~930 (2.5yr) so the always-on manager keeps
   the FULL clean universe (not just recent members) at full depth as membership churns. This makes
   research==production self-healing going forward.

Then rebuild the affected slice of the v1.1.1 panel + labels for the newly-covered (symbol,date)
cells (monthly-chunked, same as task #2).

## Open items before execution
- [ ] Cheap listing-date vs first-bar query to get the exact fixable count (run post-rebuild).
- [ ] Confirm Alpaca serves bars back to 2024 for the fixable names (it should — they're live).
- [ ] Decide whether to also widen the backfill set to names liquid at PAST dates but not currently
      in the universe (interacts with #9 delisted work — coordinate).
- [ ] Manager approval to execute (gated post-M1).

## Bottom line
Smaller and cheaper than first framed: ~0.7-1.3 GB, ~30-90 min, no new tooling (reuse
backfiller/backfill-manager). Low-risk, high-alignment-value. Recommend executing right after the
M1 battery (#4) lands, as the first M2 data-quality step.
