# MULTI-DAY (WEEKLY) HORIZON — VERDICT

**Date:** 2026-06-19 · **Pre-reg:** `prereg.md` (written before outcomes) · **Panel:** 248,775 obs, **498
weeks, 2016→2025, 1,519 symbols**, top-500 trailing-ADV liquid universe (point-in-time, no look-ahead),
tradeable Monday-open ≥09:35 ET entry, $1-floor + per-week winsor, 22 disappeared (delisting) names. Code:
`build_weekly.py` + `screen.py` (commits on the PR #205 branch). Results: `screen_results.csv` / `screen_console.txt`.

## TL;DR — the reversal signal is REAL and clean, but DIES on transaction cost. The thesis is falsified for this surface.

The weekly horizon was chosen to attack the failure mode that killed the 5 prior intraday/overnight
direction-nulls: COST. The result is the most honest possible: weekly short-term reversal is a GENUINE,
survivorship-robust, own-vol-independent, OOS-consistent signal — the first signal across all surfaces to
survive the own-vol control + the −100% delisting haircut — **but it does NOT clear realistic transaction
cost.** The cost-amortization thesis (lower turnover → cost matters less → a weaker signal clears) is
**falsified for this signal**: even at weekly turnover the median week loses after cost.

## H1 — WEEKLY SHORT-TERM REVERSAL: real signal, NOT tradeable

| gate | number | verdict |
|---|---|---|
| raw weekly rank-IC | +0.0143 (NW-t 1.82) | weak but present |
| shuffle-z | **6.38** | ✅ significant vs the within-week label-permute null |
| BY-FDR (q=0.10) | survives | ✅ |
| **own-vol/size control collapse** | **0.965** (partial-IC 0.0138 ≈ raw 0.0143) | ✅ NOT vol-mean-reversion / size tilt — a genuine reversal |
| **OOS year-split** (≤2020 / ≥2021) | **consistent** (+0.015 / +0.014) | ✅ stable across a 5y/5y split |
| **−100% delisting haircut** (net@5bps) | +10.4 bps (vs +12.1 un-haircut) | ✅ survivorship is NOT the issue (only 22/248k disappeared) |
| **net-of-cost @5bps** | mean +12.1, **median −22.5**, win **45.4%** | ⚠️ mean positive but MEDIAN NEGATIVE |
| **net-of-cost @10bps** | **mean −7.9**, median −42.5, win 43.2% | ❌ mean goes NEGATIVE at realistic cost |

**The kill is cost, decisively.** Unlike the 5 prior surfaces, reversal passes every robustness gate — it is
a real, clean signal (own-vol-independent, OOS-stable, survivorship-robust). But the decile L/S spread is
only ~32 bps GROSS/week, and a weekly long+short rebalance pays ~4 cost-legs: at 5 bps the mean barely
clears (+12) while the **MEDIAN week is −22 bps (45% win)** — the positive mean is a thin right tail, not a
repeatable edge; at a realistic 10 bps the **mean itself is negative**. A 45% weekly win-rate with a
negative median is not a tradeable strategy.

## H2 — WEEKLY LOW-VOL: weaker, OOS-FLIPS, also cost-negative

IC +0.006 (z 3.24, FDR-survives on the shuffle) but **OOS FLIPS** (−0.007 early / +0.019 late — not a
stable factor here) and net-of-cost is **negative at every level** (−3 @5bps, −23 @10bps, median −18). Dies
on both OOS instability and cost.

## Disposition — honest null, NO escalation (the pre-committed outcome)

Per the pre-registered stop condition: the edge **dies at net-of-cost even at weekly turnover** → the
cost-amortization thesis is **falsified for this signal**, reported honestly. **NO confirmatory-replication
flag** (the stop condition only escalates a signal that survives net-of-cost; this one does not). NO
promotion. This is the 6th settled negative on the direction surface, now extended to the multi-day horizon.

WHAT'S DIFFERENT / VALUABLE: this is the FIRST signal that passed the own-vol control + the −100% delisting
haircut + OOS — i.e. it is a REAL cross-sectional reversal, not a data artifact. The single, decisive
obstacle is transaction cost on a ~32 bps/week gross spread. That sharpens the next question: the
multi-day surface is not dead, but a tradeable version needs either (a) a MUCH stronger gross signal (this
one is too weak to pay ~20 bps round-trip), (b) a lower-cost implementation (cross-sectional dollar-neutral
basket with netting / longer holding to amortize further / a more liquid mega-cap subset where cost is
<5 bps), or (c) the quote-spread cost model (DataIntegrity's quote backfill) to test whether the real
effective spread on the liquid names is below the 5-bps proxy — if real cost on mega-caps is ~2-3 bps the
mean-net picture shifts, though the negative MEDIAN remains the harder problem.

## Method / infra notes (for the adversarial auditor)
- Point-in-time trailing-20d-ADV universe per rebalance (no future-liquidity peek); tradeable Monday-open
  entry (no Friday close-to-close look-ahead); ET-anchored Int32-cast daily aggregation (the #197 DST/Int8 fix).
- own-vol/size control = within-week partial rank-IC after regressing both feature and label on vol_20d +
  log-ADV; collapse = |partial|/|raw|. Shuffle = 200-iter within-week label permute. Delisting haircut
  imputes −30%/−100% terminal return on the 22 disappeared names.
- INFRA: the daily-cache build is chunked across fresh subprocesses (caps the ~17 MB/day polars per-scan
  allocation leak — anon RSS stayed flat at ~0.30 GiB; the 13-18 GiB docker-stats figure was reclaimable
  page cache, not an OOM). The cache is a host-mounted resumable partition dir (`daily_cache/<date>.parquet`,
  2514 partitions) + `.RUN_COMPLETE` marker — crash/session-survivable, NOT ephemeral.
