# Raw-data survey — `/store/raw` (the research substrate), 2026-06-16

Quantified what is in the shared raw store now that the 1-month dataset is COMPLETE, and what it supports
for microstructure research. All numbers read from the `fp_store_real` volume (mounted read-only at
`/store` via `ops/sandbox.sh`). The live capture was NOT touched.

## Coverage (symbol-dirs × date-partitions)

| kind | symbols | days | date range | per-symbol-day schema |
|---|---|---|---|---|
| bars | **7,668** | **126** | 2025-12-15 → 2026-06-16 (~6 mo) | symbol, ts (UTC), open, high, low, close, volume, **vwap**, **trade_count** |
| trades | **7,668** (full universe) | **21** | 2026-05-18 → 2026-06-16 | symbol, ts, price, size, exchange, conditions, tape, trade_id |
| quotes | **2,504** (top-liquid) | **21** | 2026-05-18 → 2026-06-16 | symbol, ts, bid_price, bid_size, bid_exchange, ask_price, ask_size, ask_exchange, conditions, tape |

Notes:
- The `date=2026-06-16` partition is EMPTY at survey time (today, mid-capture) — exclude it; use completed
  days for any test.
- bars include extended-hours minutes (counts > 390/day below), so an RTH filter is needed for an
  intraday study.
- bars carry **vwap** and **trade_count** per minute directly — so a vwap-deviation baseline and a crude
  trade-intensity can be built from bars alone, no tick read needed.

## Density (completed day 2026-06-12, liquid names)

| sym | bars | trades | quotes | median spread (bps) | median mid px |
|---|---:|---:|---:|---:|---:|
| AAPL | 804 | 830,872 | 1,164,070 | 1.03 | 291.49 |
| MSFT | 910 | 852,396 | 580,748 | 1.55 | 387.36 |
| NVDA | 954 | 2,327,820 | 2,562,895 | 1.45 | 204.97 |
| TSLA | 948 | 1,494,134 | 910,086 | 3.03 | 398.18 |
| SPY | 908 | 885,743 | 6,520,873 | 0.40 | 740.81 |
| F | 600 | 148,004 | 116,752 | 6.77 | 14.77 |
| AAL | 706 | 202,844 | 355,952 | 6.71 | 14.89 |
| PLTR | 847 | 536,872 | 907,960 | 3.11 | 128.12 |

Sanity: spreads are economically sensible and well-ordered (SPY 0.40 bps < megacaps ~1-3 bps <
low-priced names F/AAL ~6.7 bps). Per-name-day tick counts are 10^5–10^6 — ample for minute-level
microstructure aggregation.

## What it supports for microstructure research

- **OFI (Order-Flow Imbalance, Cont–Kukanov–Stoikov) from quotes** — YES, on the top-2,504 liquid set.
  The consecutive top-of-book updates (bid/ask price + size) needed for the CKS increment are present at
  high frequency. This is the H2-RETEST substrate and the basis of the OFI feature spec.
- **Signed trade imbalance from trades** — YES, universe-wide (7,668 names). price+size+ordering support
  the tick-rule sign already implemented in `quantlib.aggregates.aggregate_trades`. (The platform's
  `trade_flow` group already exposes windowed `signed_volume`; the gap is OFI, not signed trades.)
- **vwap-deviation reversion baseline** — YES from bars (per-minute `vwap`), full 126-day depth.
- **Depth/spread dynamics** — YES from quotes (bid/ask size); `quote_spread` already exposes mean spread +
  size imbalance; an OFI feature would add the *signed-change* dimension that level snapshots miss.

## Limits / honest caveats

- Trades+quotes depth is only **21 days** — thin for a day-clustered marginal-IC test on the liquid tier.
  Requested a deepen to ~63 days from the backfill agent (depth > breadth for OFI power).
- Quotes cover the **top-2,504** only — fine (research runs on a ~150-250 liquid subset), but an
  illiquid-tail OFI study is not yet possible (and the illiquid tail historically failed the cost gate —
  LEADS.md H1).
- Survivorship: this is the CURRENT universe over a recent month — no delisted names. Fine for a
  short-horizon intraday microstructure study; NOT for a multi-month survivorship-sensitive claim.
