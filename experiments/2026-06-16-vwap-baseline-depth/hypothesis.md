# Hypothesis (PRE-REGISTERED — written before running) — vwap_dev baseline at DEPTH + H1-recheck

**Author:** Modelling Agent · **Date:** 2026-06-16 · **Resource:** CPU-only (sandbox, MEM=16g CPUS=8 grant).

## Why this cycle (it is NOT idle filler)
The H2-RETEST (top lead) is **blocked on the trades backfill** — `/store/raw` currently has BARS only
(578 symbols × 126 days, 2025-12-15→2026-06-16), no trades/quotes yet. So H2-RETEST and H3 cannot run.
But two open questions are answerable on bars ALONE at proper power, and BOTH de-risk the pending H2-RETEST:

1. **The H1 kill was on a single Monday session** (vwap_dev reversion was illiquid-concentrated, illiq/liq
   |IC| 2.06×/4.01×). Was that a one-day artifact, or does it hold at depth? This decides whether H1 stays
   dead AND tells us where the reversion lives (load-bearing for cost work).
2. **The H2 cycle's vwap_dev baseline was UNDER-POWERED on 3 days** (IC −0.0044, inside canary), which is
   exactly why the OFI marginal-lift question came out ambiguous. The H2-RETEST needs a TRUSTWORTHY,
   properly-powered vwap_dev baseline IC to measure OFI's lift AGAINST. Establish it here, on 126 days.

This cycle produces the vwap_dev baseline number the H2-RETEST will orthogonalize against — direct prep.

## Data (READ-ONLY via ops/sandbox.sh; /store/raw bars)
- `/store/raw/bars/symbol=*/date=*/*.parquet`, cols `symbol, ts, open, high, low, close, volume, vwap, trade_count`.
- **578 symbols × 126 trading days.** RTH ONLY: filter `ts` to 13:30–20:00 UTC (drop pre/post-market rows).
- `vwap_dev = close/session_cumVWAP − 1` (session cumulative VWAP from bars within each day).
- Forward return TRADEABLE: enter t+1 close, exit t+H close (`fwd = close(t+H)/close(t+1) − 1`); H=15 and
  H=30. Cross-sectionally demean fwd within each (date, minute).
- Liquidity = trailing dollar-volume (`close*volume`, rolling ~30m) per symbol at t.
- **NO writes to /store. Scratch only.** (Bars are read directly from the read-only mount — no Alpaca fetch.)

## Method
Pool across (date, minute) cross-sections:
1. **Powered vwap_dev baseline:** within-minute Spearman rank-IC of `vwap_dev` vs demeaned fwd-ret, H=15 & 30.
   Mean IC + t (per-day IC std / sqrt(n_days) — day-clustered, the honest t for a multi-day panel).
2. **H1-recheck:** split each cross-section into liquidity terciles (or halves) by trailing dollar-volume;
   report within-minute IC per tier. Compute the illiquid/liquid |IC| ratio at depth.
3. **Shuffle canary** (multi-seed): permute fwd within minute → IC ~0.
4. **Crude turnover/net-of-cost** read for the vwap_dev decile-L/S book (turnover × ~2bps), per tier — does
   ANY liquidity tier clear its own cost? (the H1 economic question, now at depth.)

## EXPECTED result (committed BEFORE running — the falsifier)
- **B1 (baseline, conf ~80%):** powered vwap_dev rank-IC is NEGATIVE and SIGNIFICANT at depth, magnitude
  ~−0.02 to −0.04 (consistent with the historical 0.028 and the single-day −0.048/−0.028). A near-zero or
  positive baseline at 126 days would contradict the entire standing carrier story (key falsifier).
- **B2 (H1-recheck, conf ~60%):** the illiquid/liquid |IC| ratio stays > ~1.5 (reversion still stronger in
  illiquid names) — confirming the H1 kill was NOT a single-day artifact. If instead the ratio collapses to
  ~1 at depth (reversion is UNIFORM across liquidity), then the single-Monday illiquid-skew WAS an artifact
  and **H1 should be RE-OPENED** (a liquid subset might clear cost after all). I lean toward the kill holding
  (~60%), but I am genuinely testing it — a ratio ~1 flips H1 back to live.
- **B3 (economics, conf ~70%):** NO liquidity tier's vwap_dev book clears its own ~2bps cost net (breakeven
  < cost in every tier) — the "real but uneconomic at turnover" verdict holds at depth. A tier that clears
  cost net would be a genuine surprise and an H1 re-open.
- **Pre-committed honesty:** 578 names is survivorship-tilted (alive-today) and the OFI/trades layer is
  absent, so this cycle CANNOT claim edge — it establishes the powered baseline + settles the H1 artifact
  question. Those are the deliverables; an economic edge is not expected and not the bar.
