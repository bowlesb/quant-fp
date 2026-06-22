# BACKFILL-PRIORITY RECOMMENDATION — for Ben's budget decision (order-flow lane)

**TL;DR — the order-flow backfill is NOT the gate; it is already landed.** The premise that the order-flow
tape is Ben-budget-gated and not landed is **STALE**. The broad raw trade + quote tapes are on disk and cover
the same window the cross-sectional screens use. The only genuinely data-gated item is the disjoint-quarter
OOS replication (G5), and only IF the cheap screen first goes GO. Recommendation: **run the screen first
(spend $0); ask for one extra quarter of breadth ONLY if a G0 framing earns it.**

## Verified data-state (read-only survey of `fp_store_real`, 2026-06-22)

| layer | broad breadth begins | broad syms/day | head depth | head syms |
|---|---|---|---|---|
| `raw/trades` | **2026-03-30** | **~7,608** | back to ~2024-12 | ~855 |
| `raw/quotes` | **2026-03-30** | **~4,042** | back to ~2024-12 | ~542 |
| `raw/bars`   | full 18 mo | ~7,690 | — | — |

- Trade schema verified: `symbol, ts(µs-UTC), price, size, exchange, conditions, tape, trade_id` — 993,586
  prints for AAPL on a single day. Quotes overlap the trades → tick-level Lee-Ready signing + realized
  effective spread are computable NOW.
- The broad trade+quote window is **~55 trading days, 2026-03-30 .. 2026-06-18** — the SAME window the
  quote-tape G0 and path-geometry G0 used. The screen runs immediately.
- The "trades thin ~2k/day" prior is stale (memory `project-deep-raw-history-foundation`) — broad trades are
  ~7.6k/day. (Memory updated.)

## What runs NOW vs what is data-gated
- **Runs now on the landed window (spend $0):** the entire G0a + G0b screen, plus G1 (own-vol), G2
  (incremental over all existing order-flow/quote groups), G3 (shuffle), G4 (BY-FDR), G-STALE (tick
  no-look-ahead), G6 (bit-identical). The effective-cost model (the likely KEEPER) ships on this window
  exactly as the quoted G0b model did.
- **Data-gated — needs a backfill:** ONLY G5, the genuinely-disjoint-quarter OOS replication. The broad tape
  is a single ~3-month regime; a within-window split is not a separate regime. G5 is the only gate that needs
  more data, and it is only reached if a G0 framing goes GO.

## The precise ask (CONDITIONAL — do not pre-spend)

**Priority 1 — NOTHING. Run the screen first.** Free. If both G0 framings null (prior art predicts a G0a
null), there is no backfill to fund.

**Priority 2 — IF a G0 framing goes GO (or the Lead wants cross-regime cost-model validation):** extend the
**trade + quote BREADTH backward from 2026-03-30 to 2026-01-01** (one prior quarter), ideally **2025-10-01**
(two quarters), so G5's held-out window is a separate regime.
- This is a **breadth-at-depth** fill: the ~6,750 non-head trade names / ~3,500 non-head quote names absent
  before 2026-03-30. It is NOT new head dates — the ~855 head trade / ~542 head quote names already reach
  ~2024-12.
- One quarter ≈ ~6,750 syms × ~63 trading days of trades + ~3,500 syms × ~63 days of quotes. Liquid names are
  ~1M prints/day; the long tail far fewer. Two quarters ≈ 2×.

## Rough cost
- **Data:** $0 marginal — Alpaca gives unlimited historical access (memory
  `feedback-alpaca-first-and-crypto-canary`); the manifest dedups so existing partitions are not re-downloaded.
- **Compute / storage / run-time:** a bounded, staged, `quant-backfill`-named job (memory
  `reference-backfill-memguard-name` — the live_monitor mem/disk guard only protects a container literally
  named `quant-backfill`). NVMe disk-check before writing. Produce the exact byte/row estimate via a
  `--dry-run` manifest-diff BEFORE any fetch.

## Net steer for Ben
1. Greenlight the cheap screen (free, runs now). Decide the backfill only on its result.
2. If a G0 framing is GO → fund the one-quarter (ideally two-quarter) breadth-back extension for G5 — a
   bounded staged fill, $0 data, modest compute. Get a `--dry-run` estimate first.
3. Do NOT run a large speculative backfill. The lane's whole value is screened cheaply on data we already have.
