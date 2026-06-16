# Method — vwap_dev baseline at depth + H1-recheck

Research script: `run.py` (CPU-only, run via `ops/sandbox.sh`, MEM=16g CPUS=8). READ-ONLY on
`/store/raw/bars`. No writes to /store. NOT engine code; will not be merged.

## Panel actually processed
- Source: `/store/raw/bars/symbol=*/date=*/*.parquet`, cols `symbol, ts(UTC), open, high, low,
  close, volume, vwap, trade_count`.
- **n_dates = 126** (all available, 2025-12-15 → 2026-06-16).
- **n_symbols_seen = 629** (the store has MORE than the 578 named in the pre-reg — it has grown;
  I processed every symbol dir present rather than capping at 578, and report the real count).
- **Grid rows processed (symbol-minute) = 26,679,510.**
- Valid cross-sections (clock-minutes with ≥20 ranked names & finite fwd): **46,873 (H=15)**,
  **44,998 (H=30)**.
- Sharded by DATE: one date loaded, gridded, IC-accumulated, then dropped. Peak memory well under
  16g; full panel ran in ~4 min, no OOM, no subsampling needed.

## RTH filter
`ts` seconds-of-day filtered to `[13:30:00, 20:00:00)` UTC (regular session). The raw bars include
pre/post-market rows (09:xx, 11:xx, 21:xx UTC observed) — these are dropped. Cross-sections built on
RTH bars only.

## Grid / gap handling (DOCUMENTED choice)
Bars are sparse (only minutes with trades exist). For strict wall-clock comparability of
cross-sections, each (symbol, date) is **reindexed to the full 390-minute RTH 1-min grid**
(minute-index 0..389). Per missing minute:
- `close` is **forward-filled** (then back-filled for any leading gap) — a symbol carries its last
  traded price into untraded minutes.
- `volume` filled to **0**; `vwap` filled to `close` for the cumVWAP product → an untraded minute
  contributes 0 to the running cumVWAP sums.

This is the simplest honest choice and is made explicit because it has a KNOWN side effect (see
"Cost caveat" below): in thinly-traded names, a forward-filled stale close that "jumps" when the next
real trade prints manufactures mechanical mean-reversion that is **not tradeable**. We flag every
result this contaminates.

## Features
- **session cumVWAP_t** = `cumsum(vwap*volume) / cumsum(volume)` over RTH bars up to & incl. t
  (denominator floored at 1 to avoid div-by-zero before first volume).
- **vwap_dev_t** = `close_t / cumVWAP_t − 1`.
- **Liquidity proxy** = trailing **30-minute rolling sum of dollar-volume** (`close*volume`) per
  symbol at t (`min_samples=1`).

## Forward return (TRADEABLE, look-ahead care)
`fwd = close(t+H)/close(t+1) − 1` — **enter at t+1 close, exit at t+H close** (never the signal bar
t itself; avoids using the t→t+1 move that the signal is contemporaneous with). H ∈ {15, 30}, in
**1-min grid bars** (= wall-clock minutes given the grid). A minute m gets a fwd only if `m+H ≤ 389`
(both t+1 and t+H exist on the grid) → no peeking past the session end.
`fwd` is **cross-sectionally demeaned within each (date, minute)** before IC.

## IC + day-clustered t
- Within each (date, minute) cross-section (≥20 finite names): **Spearman rank-IC** of vwap_dev vs
  demeaned fwd. Spearman implemented numpy-only (average-rank `_rankdata` for ties, Pearson on ranks;
  scipy is not in the sandbox image — verified equivalent on ties).
- **Pooled mean IC** = mean over all valid minute-ICs.
- **Day-clustered t** (the honest t for a multi-day panel): per-DAY mean IC over its minutes, then
  `t = mean_over_days / (std_over_days / sqrt(n_days))`, n_days=125 effective (1 day had <2 usable
  minutes for the ddof=1 std). Per-minute t would massively overstate (minutes within a day are
  autocorrelated); day-clustering is reported throughout.

## H1-recheck (liquidity tiers)
Each cross-section split into **3 terciles** by the trailing-30m dollar-volume proxy
(tier 0 = illiquid … tier 2 = liquid), via argsort + `array_split`. Per-tier within-minute IC
accumulated and day-clustered identically. Headline = **illiquid/liquid |IC| ratio**.

## Shuffle canary
For every cross-section, `fwd` is permuted within the minute (**10 seeds**, fixed RNG) and IC
re-measured. Pooled canary mean/std reported — must be ≈0.

## Crude net-of-cost (per tier)
Within each liquidity tier, an **equal-weight decile L/S book on vwap_dev**: long the cheap decile
(low vwap_dev), short the rich decile (high vwap_dev); per-period book return = mean(bottom-decile
fwd) − mean(top-decile fwd). Cost model (crude, stated): rebuild the book every period, 2bps one-way,
2 sides, entry+exit ⇒ **4 × 2bps = 8 bps/period round-trip** on notional. `net = gross − cost`.

**COST CAVEAT (load-bearing):** the illiquid-tier gross is dominated by the ffilled-stale-close
artifact above — its "tradeable" t+1 close is fictional (no real trade that minute), so its
clearing cost is an artifact, NOT a real edge. The only tier where the entry/exit closes are real
prints is the **liquid** tier; that is the economically meaningful net read.
