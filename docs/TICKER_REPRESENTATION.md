# Ticker-representation analysis — quote/trade-tape backfill priority

**Read-only analysis (Warehouse, 2026-06-20).** Which *tradeable* names are under-represented in the raw
tape, ranked as a backfill-priority list for DataIntegrity's quote/trade-tape deepening. Answers #204's open
question directly: deepen **EARLIER** (more history for covered names) vs **WIDER** (more tradeable names with
any coverage).

## Method (read-only, no schema change)

- **Tradeable universe** = the latest-date (2026-06-18) ADV-rank bands from the deep+broad **bars** tape — the
  same `liquidity_bands` cut every lane uses (top-500 B1 / 500-1k B2 / 1k-2k B3 / 2k-4k B4 / 4k+ B5; ADV =
  trailing-20d RTH dollar volume, point-in-time, ≥21 trailing bars). **6593 ranked tradeable symbols.**
- **Coverage** = per-symbol depth/breadth from the **quotes** and **trades** raw manifests (`rows>0` real
  cells). Bars are already deep+broad (2016→, ~4474 sym/day) — the gap is quotes/trades, the layers DI is
  actively deepening.
- Reproduce: the bounded ADV scan + manifest join is a ~1-min read-only sandbox (no live touch, no store
  write). Script lived in `/tmp` (not committed, per CLAUDE.md).

## Headline — quotes are the bottleneck; trades breadth is complete

| layer | tradeable names with NO coverage | tradeable core (B1+B2) ≥half-depth |
|---|---|---|
| **trades** | **0 / 6593** (breadth complete) | depth: B1/B2 = full 379d; B3-B5 median ~63d |
| **quotes** | **2433 / 6593** (37% have zero quotes) | **only 499 / 1000 (50%)** have ≥half-depth quotes |

Quote depth anchor = 189 dates (2025-09-18 → 2026-06-18); trades = 379 dates.

### Quote coverage by liquidity band

| band | tradeable | have quotes | NONE | median depth where present |
|---|---|---|---|---|
| B1 (top-500) | 500 | 500 (100%) | 0 | 188d |
| B2 (500-1k) | 500 | 500 (100%) | 0 | 62d |
| B3 (1k-2k) | 1000 | 1000 (100%) | 0 | 62d |
| B4 (2k-4k) | 2000 | 1894 (95%) | **106** | 63d |
| B5 (4k+) | 2593 | 266 (10%) | **2327** | 1d |

Two distinct gaps, both real:
1. **DEPTH** (the liquid core): B1 has full breadth but the median name only reaches back ~62-65d (to
   ~2026-03-18) — only the very top of B1 has the full 189d. **Half the top-1000 tradeable names have a
   shallow quote tape.** This is the EARLIER axis: extending the quote floor below 2025-09-18 deepens the
   names we most want to trade.
2. **WIDTH** (the B4/B5 tail): 106 B4 names + 2327 B5 names have NO quotes at all. This is the WIDER axis —
   but most of the missing 2327 are B5 (illiquid 4k+), where a thin quote tape is lower-value.

## Recommendation for DataIntegrity — EARLIER first, then WIDTH at B4

The #204 saturation finding (top-500 × 2025-09-18→2026-03-17 already full) is consistent with this: the top
names are breadth-covered, so re-fetching that window adds nothing. The high-value moves, ranked:

1. **DEEPEN the liquid core EARLIER (highest value).** Extend the quote fetch *below the 2025-09-18 floor* for
   **B1+B2 (top-1000 ADV)** — 50% of them have only ~62d. This buys quote history on exactly the names a
   strategy trades, and it's where parity-trustable quote features can actually earn trust. Earlier > wider.
2. **WIDEN to the 106 zero-quote B4 names (second).** These are mid-liquidity tradeable names (rank
   2000-4000) with trades + bars but no quotes — a cheap, bounded breadth win (only 106 names) that lifts B4
   quote coverage 95% → 100%. Ranked list below.
3. **DEPRIORITIZE the B5 tail (2327 zero-quote).** Illiquid 4k+; thin quote tape is low tradeable-universe
   value. Fetch opportunistically, not as a priority.

Trades need no width work (0 missing); the only trades gap is B3-B5 *depth* (~63d), a lower priority than the
quote core.

## Ranked backfill-priority lists

### WIDTH — most-liquid tradeable names with ZERO quote tape (top 30 of 2433; the B4 cheap win)

All are B4 (rank 2000-4000) with trades + bars but no quotes. `OBAI #2898, WKSP #2925, ALOT #2950, FTHM
#3246, SVAC #3248, INLF #3478, LNAI #3487, GPAT #3572, QLEP.U #3578, PRTH #3696, INTG #3732, EXFY #3799, CZWI
#3802, FTA #3823, WYY #3842, SIF #3844, BRW #3845, CII #3847, ISOU #3854, SCM #3856, BAC.PRO #3858, FLG.PRU
#3864, SLN #3870, LYEL #3874, XFOR #3875, KF #3876, …` (full 106-name B4 set regenerable from the script).

### DEPTH — most-liquid B1 names with a SHALLOW quote tape (<50% of the 189d anchor; top 20 of 3660)

These are top-500 ADV names whose quotes only reach ~2026-03-18 — the EARLIER-deepening targets:
`CBRS #77 (24d), RDW #99 (65d), NVTS #110, FLEX #151, FUTU #185, NTAP #192, SMTC #194, PL #197, LUNR #204,
STRL #218, TWLO #221, WOLF #223, VSH #226, ENPH #228, GFS #240, NUVL #245, TE #249, POET #251, TTMI #252, HUT
#260, …` (all reach back only to ~2026-03-18 ≈ 62-65d vs the 189d anchor; CBRS shallowest at 24d).

## Notes

- ADV rank is point-in-time on 2026-06-18 bars; bands shift slightly day to day (a name near a cut can move).
  The priority *tiers* (B1+B2 core depth, B4 width) are stable; treat individual ranks as approximate.
- B5's 4k+ tail and the `.U`/`.PR` suffixes (units/preferred) overlap the sector-coverage "unknown" tail —
  low tradeable-universe value, consistent with deprioritizing them.
- This complements `docs/BACKFILL_SCOPE.md` (which covers BARS depth vs the live universe — already good) by
  measuring the QUOTE/TRADE tape vs the tradeable ADV universe, the current deepening front.

## Status — B4 WIDTH landed; the NEXT tranche is the tight-spread LP head

- **B4 width DONE (2026-06-20).** The 31 remaining zero-quote B4 targets (`OBAI/WKSP/AZTR/WPRT/LNAI/ALOT/…`,
  computed by `quantlib.data.b4_quote_widen`) were fetched over the full quote span — all 31 landed (379
  date-partitions each, 11,749 partitions / 10,541 real symbol-days / 0.217 GB; verified against the quotes
  manifest). B4 quote coverage is now ~100%. The quote tape spans 2024-12-12 → 2026-06-18 across 4,323
  symbols.
- **NEXT = the tight-spread LP tranche (the liquid head, not the tail).** With the B4 width closed, the
  remaining #208 quote-acquisition value is DEPTH on the names a liquidity-provision / spread-capture strategy
  could actually trade: the mega/large-cap liquid head whose median quoted spread sits in the **1-5bps** band.
  `universe_membership.median_spread_bps` is NULL (never seeded), so the tranche is measured DETERMINISTICALLY
  from the deep-quote panel itself by `quantlib.data.next_quote_tranche` — rank the bars universe by ADV, take
  the liquid head, measure median `(ask-bid)/mid` (bps) + an LP-headroom proxy (median top-of-book size) over
  a recent quote sample, keep the 1-5bps band, rank deepest-headroom first. Names below 1bps (e.g. NVDA at
  ~0.98bps) are excluded — penny-spread, no provision edge; wider names (AMD ~6.6bps, ORCL ~5.9bps) are out of
  band. A representative top-200 cut yields ~73 in-band names (AAPL/MSFT/AMZN/GOOGL/META/INTC/NFLX/PLTR/…).
  These already HAVE quote coverage, so the driver `ops/quote_tranche_lp.sh` is an idempotent REFRESH that
  only brings each name's tape current — guard-named `quant-backfill-quotes-lptranche`, one-at-a-time,
  memory-bounded. Run `ops/quote_tranche_lp.sh --dry-run` to preview the ranked tranche before launching.
