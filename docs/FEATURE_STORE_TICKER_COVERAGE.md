# Feature-store ticker coverage (stream vs backfill, per symbol)

A read-only, symbol-centric view of WHICH tickers the FEATURE STORE covers, HOW BROADLY (across the 51
feature groups, stream vs backfill), and HOW DEEPLY (how far back the settled backfill reaches). It is the
inverse of the dashboard's per-GROUP coverage surfaces (`services/dashboard/feature_grid.py`,
`store_grid.py`), which answer "for this group, which symbols?"; this answers "for this symbol, how is it
represented across the whole store, and is it under-represented LIVE vs settled?".

This is distinct from `docs/TICKER_REPRESENTATION.md`, which ranks raw QUOTE/TRADE-TAPE backfill priority off
the raw manifests against the ADV universe. This note is about the computed FEATURE STORE
(`group=…/source=stream|backfill/date=…` partitions), not the raw tape.

Produced by `ops/analyze_ticker_representation.py` (read-only; reads partition directory names + a bounded
sample of `symbol` columns; never writes the store, changes schema/format, or touches a fingerprint). The
host has neither polars nor the store mount, so run it inside a container that has both:

```bash
docker exec quant-dashboard-1 python /app/ops/analyze_ticker_representation.py --store /store
docker exec quant-dashboard-1 python /app/ops/analyze_ticker_representation.py --store /store --json
```

## Findings (fp_store_real, anchor 2026-06-18, 51 groups)

| Metric | Value |
|---|---|
| Symbols seen anywhere | 11,394 |
| Present in a recent settled backfill date | 7,152 |
| Stream-only (live but never settled-backfilled) | 3,684 |
| Under-represented LIVE (backfill-present, stream-absent in ≥1 group) | 7,142 |
| Symbols with any settled history | 8,436 |

### 1. Under-representation is the FP_TICK_SYMBOLS order-flow gap — and it bites LIQUID names

The under-representation is NOT random tail noise. The most-under-represented LIVE symbols split into two
shapes:

- A handful of recent IPO / warrant tickers (e.g. `BLRKW`) missing from ~40 groups — genuinely new, not a
  capture gap.
- A large block of **fully-liquid blue chips** — `CAT`, `BKNG`, `AEP`, `AMGN`, `AMCR`, `AMH`, `CART`,
  `DRI`, `EFX`, `BYD`, `BRO`, `EEFT`, `GIL`, … — present in **all 51 backfill groups** and **45 stream
  groups**, missing from **exactly the same 6 stream groups**. Those 6 are the per-trade tick / order-flow
  groups (`trade_flow`, `signed_trade_ratio`, `inter_arrival`, `tick_runlength`, `trade_size_dist`,
  `volume_exhaustion` and siblings) gated by `FP_TICK_SYMBOLS` — unset, they stream only the ~canary floor
  while their backfill is universe-wide.

So the dominant under-representation signal is the **same `FP_TICK_SYMBOLS` live-capture gap** the warehouse
has tracked per-group (PR #121 / #127 / #140), now confirmed at the symbol level: it is starving the live
tick features of the most-tradeable names, not just the tail. Widening `FP_TICK_SYMBOLS` to cover the liquid
head closes the bulk of the 7,142-symbol under-rep count where it matters for tradeable signal.

### 2. History depth is bimodal

| Backfill span band | Symbols |
|---|---|
| ≤7d | 39 |
| 8–30d | 51 |
| 31–90d | 665 |
| 91–180d | 0 |
| 181–365d | 23 |
| >365d | 7,658 |

The vast majority (7,658 of 8,436) reach back over a year in at least one group, confirming the deep
backfill foundation. The shallow band (≤90d, ~755 symbols) is overwhelmingly recent IPOs, warrants, and
preferred-share tickers (`FITB.PR*`, `FCBM`, `FISN`, `FRBT`, …) — shallow because they did not exist
earlier, NOT because of a capture gap, so they are LOW backfill-priority. The empty 91–180d band is a
sampling artifact of the bounded depth pass (see Method), not a real hole; trust the band edges, not the
interior resolution.

## Backfill-priority recommendation

1. **Highest leverage is LIVE, not historical: widen `FP_TICK_SYMBOLS`** so the 6 order-flow groups stop
   dropping `CAT`/`BKNG`/`AMGN`-class names on the stream. This closes the bulk of the under-representation
   and is a live-capture config change, not a backfill job. (Routed to the order-flow / live-capture owner,
   not the warehouse backfill queue.)
2. **The shallow-history tail is mostly legitimate** (recent listings / non-common share classes). No broad
   backfill-deepening is warranted for it — deepening cannot extend a name that did not trade earlier.
3. **Stream-only symbols (3,684)** are live but never settled-backfilled: candidates for a settled backfill
   pass IF universe-relevant. Cross-reference the ADV / liquidity bands (`docs/LIQUIDITY_BANDS.md`,
   `docs/TICKER_REPRESENTATION.md`) before queueing — most are deep-tail illiquid names not worth settling.

## Method & honest limitations

- **Breadth / under-representation** is read on the most-recent settled backfill dates (where every
  currently-traded name appears) and the recent stream window — an accurate present-day group count and a
  faithful stream-vs-backfill gap.
- **Depth** samples a bounded set of dates per group (earliest + latest edges + an evenly-spaced interior
  sample). The earliest sampled date a symbol appears on is a tight UPPER bound on its true first
  appearance, so depth bands are accurate at their edges; the interior resolution is coarse by design.
  Reading every date of every multi-year group would be minutes of I/O for marginal precision.
- Per-partition symbol sets use a bounded evenly-spaced file sample (the stream writes thousands of
  per-minute files whose symbol universe is ~constant across the day) — the same proven approach as
  `feature_grid._read_symbols`.
- Read-only throughout; no schema / format / fingerprint change.
