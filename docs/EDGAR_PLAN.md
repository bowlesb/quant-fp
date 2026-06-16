# EDGAR as a first-class parity-true feature source — assessment & roadmap

> **Status: ASSESSMENT (2026-06-16).** Prioritized, architecture-aligned plan to take SEC EDGAR filings
> from *collected-but-unconsumed* to a **first-class PARITY-TRUE event-feature source on the bus feeding
> strategy containers**. Companion to `docs/EDGAR_INGESTION.md` (the Phase-1 ingestion design + as-built).
> This doc proposes; it does not build. Phase 1 below is the smallest slice that proves the whole spine.

## TL;DR

EDGAR is **half-wired**. The ACQUIRE layer is built and correct on the hard part (point-in-time
`available_at`), but **nothing materializes filings into features, nothing reaches the bus, and the
collector is not running** — the `filings` table is empty. The #1 next slice is a single parity-true
event-clock feature (`minutes_since_last_8k`) that closes the spine end-to-end: backfill reads stored
`available_at`, live folds a counter, both emit the identical value onto the bus. The biggest risk is
**parity for sparse event state across the warmup/session boundary** — the event clock spans
non-market-hours and most minutes have no filing, so null/seeding discipline must be exact.

---

## 1. Current EDGAR state (concrete)

### What exists and is correct
- **ACQUIRE / live ingest** — `services/edgar/main.py` (stream + backfill modes). Polls the SEC
  current-filings Atom feed ~5s, dedupes by accession, maps CIK→ticker, UPSERTs into `filings`. Backfill
  mode walks `data.sec.gov/submissions/CIK{cik}.json` for a symbol list.
- **Store** — `db/init/08_filings.sql`: `filings` TimescaleDB hypertable partitioned on `available_at`,
  indexes on `(symbol, available_at DESC)`, `(cik, available_at DESC)`, `(form_type, available_at DESC)`.
- **The point-in-time fix is real** — three separated timestamps (`filed_at` / `accepted_at` metadata,
  `available_at` the look-ahead-safe contract field), `available_at_source` confidence flag, live path
  sets `available_at` only. This is the single hardest thing to get right and it is right.
- **Shared rate limiter** — `quantlib/sec_rate_limit.py` (token bucket ~4 rps) used by both paths.
- **Tests** — `tests/test_edgar_ingest.py` (parsing, mapping, 3-timestamp parity, unmapped-CIK retention).

### What does NOT exist / is not running
- **The collector is not running.** `docker ps` shows feature-computer, smoke-strategy, reversion-strategy,
  quant-backfill, quant-redis, quant-timescaledb — **no `edgar` container**. The `filings` table is
  **empty (0 rows)**. Data is not accruing today.
- **Zero consumers.** No feature group reads `filings` (grep of `quantlib/features/groups/` for
  `filings`/`edgar`/`available_at`/`cik`/`8-K` → 0 hits). The only `*_since_*` features are
  `minutes_since_open` (calendar) and `minutes_since_pivot` (swing) — neither is filing-based.
- **No event-clock state kind.** `quantlib/features/stateful.py` has `EMAState` (recursive) and
  `LastKState` (lag ring); there is no per-(symbol, form_type) event-counter state.
- **No raw EDGAR store.** Filings land straight in Postgres; there is no `/store/raw/edgar/...` parquet
  mirror parallel to bars/trades/quotes, and no manifest/resumable discipline for a historical sweep.
- **No EDGAR bus fields / no EDGAR strategy.** Filings never reach `quantlib/bus/` or any strategy container.

### Verdict
EDGAR data is **flowing to a store in principle but not in practice (collector down, table empty), and not
at all into the feature platform.** The gap is the entire MATERIALIZE and CONSUME half of the spine, plus
turning the collector back on and giving it a resumable historical backfill.

---

## 2. How EDGAR filings become point-in-time event features

The platform spine is **ACQUIRE → MATERIALIZE → CONSUME**, and everything of value is a feature whose
live `compute_latest()` equals backfill `compute()` cell-for-cell (`docs/PARITY_COVERAGE.md`,
`tests/test_fp_latest.py`). EDGAR fits this exactly if we treat a filing as a timestamped **event** keyed
on `available_at` and never on `filed_at`.

### The parity construction (why live == backfill by design)
A `filings` row is `(symbol, form_type, available_at)`. For any feature minute `T`:
- **Backfill `compute()`** reads, per symbol, `max(available_at) WHERE available_at <= T` and derives the
  feature from `T - that`. Pure point-in-time SQL/polars over stored rows.
- **Live `compute_latest()`** folds a per-(symbol, form_type) counter: each minute increment; when a row's
  `available_at` falls in this minute, reset to 0. The state reached by folding minute-by-minute equals
  `T - max(available_at <= T)` — identical to backfill **because both key off the same `available_at`**.

This is the same `seed(H); fold(m) == seed(H+m)` invariant that makes `ReductionGroup` / `EMAState` /
`LastKState` parity-true (`quantlib/features/stateful.py`, `tests/test_fp_stateful.py`). The event clock is
a **new state KIND** under that same abstraction, not a side hack — consistent with the "state abstraction,
no corner-cuts" principle.

### Look-ahead discipline (the EDGAR-specific traps)
- **Key off `available_at` only.** `filed_at` is often a bare date (midnight) and leaks look-ahead; using
  it would be the filing-data analogue of the tradeable-entry trap.
- **The clock spans non-RTH.** A 2am 8-K affects the 09:30 open. Unlike RTH-scoped intraday features, the
  event clock runs continuously across the warmup/session boundary. Seeding must include overnight filings.
- **Amendments are their own events** (`8-K/A`, `10-K/A`) with their own `available_at` — never backfill an
  amendment's time onto the original.
- **Point-in-time CIK↔ticker.** Companies rename; map a filing to the ticker valid at its `available_at`
  and historize the map (same discipline as `sector_map` historization). The as-built applies today's map
  to all history — a known gap to close (low impact for Phase 1's large-cap set, must fix before breadth).

### Concrete features and their Case (A = derived from data already in a store; B = needs a new input)

All EDGAR features are **Case B** today because `filings` is a *new input frame* the engine does not yet
read (no `InputSpec(name="filings", ...)`). Once a `filings` input frame exists, downstream content
features built on the same frame are Case-A-like (no new ingest, just new math). Grouping:

| Feature | Group | Phase | Notes |
|---|---|---|---|
| `minutes_since_last_8k` | event_clock | **1** | `T - max(available_at)` for `form_type LIKE '8-K%'`; null if never. **The Phase-1 slice.** |
| `minutes_since_last_filing` | event_clock | 1 | any form; the generic clock |
| `is_within_30m_of_8k` | event_clock | 2 | binary gate, derived from the clock |
| `filings_today` | event_clock | 2 | count since session start (RTH-scoped counter) |
| `form_8k_flag` / `form_10x_flag` / `form_4_flag` | event_clock | 2 | "a filing of type X disseminated this minute" |
| `minutes_since_last_form4` | event_clock | 2 | insider-activity clock |
| `filing_8k_has_earnings` (Item 2.02) | filing_content | 3 | port 8-K item regex; attaches to event, decays |
| `filing_8k_has_guidance` (7.01) / `_officer_change` (5.02) / `_acquisition` (2.01) | filing_content | 3 | item flags |
| `insider_buy_flag` / `insider_net_shares_log` | filing_content | 3 | Form-4 XML parse; direction + magnitude |
| `filing_surprise` | filing_content | 4 | needs fundamentals (consensus vs reported) — later breadth phase |

The Phase-1 deliverable is the **first row only** (`minutes_since_last_8k`), proven on the bus.

---

## 3. ACQUIRE — raw EDGAR backfill + live ingest (mirror the bars/trades/quotes pattern)

The live ingest exists (`services/edgar/main.py` stream mode). Two gaps to close to match the platform's
acquisition discipline:

### 3a. Turn the collector on and let `filings` accrue
The collector is a tiny service already wired in `docker-compose.yml`. The immediate, near-zero-risk act
is to run it so the point-in-time store starts filling — this is a precondition for **live==backfill
parity to even be testable** (you need recorded live `available_at` to compare a backfill against).

### 3b. A resumable historical raw EDGAR backfill (mirror `raw_backfill.py`)
The market-data ACQUIRE layer writes parquet partitions under `/store/raw/<tier>/symbol=…/date=…/` with an
append-only manifest (`/store/raw/_manifest_<tier>.d/part-*.parquet`, schema
`tier,symbol,date,rows,bytes,fetched_at`) and idempotent resume via `done_keys()`
(`quantlib/data/raw_backfill.py`, `quantlib/data/raw_store.py`). EDGAR should mirror this so historical
filing pulls are **resumable and parity-auditable**, not a fragile one-shot:

- **Raw store:** `/store/raw/edgar/symbol=<S>/year=<YYYY>/filings.parquet` (or `cik=<CIK>/...` for
  unmapped). Columns mirror the `filings` schema, `available_at` flagged `submissions_accepted`
  (lower-confidence) for deep history predating live collection.
- **Manifest:** `/store/raw/_manifest_edgar.d/part-*.parquet` keyed `(cik|symbol, year)`; resume skips
  done keys; empty years written as zero-row partitions (matches the bars convention).
- **Loader → `filings`:** a small materializer reads the raw parquet into the `filings` hypertable with
  `ON CONFLICT (accession_number, available_at) DO UPDATE` (dedup across live + backfill already coded),
  `COALESCE`-filling `symbol` for late CIK resolution, **never** downgrading a live `atom_feed`
  `available_at` to a backfill `submissions_accepted` one.
- **Rate discipline:** reuse `quantlib/sec_rate_limit.py` (already shared); a backfill sweep at 4 rps over
  the universe is the long pole — budget it like the trades/quotes tiers.

This keeps EDGAR a peer of bars/trades/quotes in ACQUIRE: a raw immutable parquet layer + a resumable
manifest + a materializer into the queryable store.

---

## 4. MATERIALIZE — the event-clock feature group

Add `quantlib/features/groups/event_clock.py`, decorated `@register` (the registry pattern: one new group
file, no edits to shared modules — `quantlib/features/registry.py`, `docs/ADDING_A_FEATURE.md`):

- **Input:** a new `InputSpec(name="filings", columns=("symbol","form_type","available_at"))`. This is the
  one genuinely new wiring: the feature engine must build a `filings` frame (keyed by symbol, minute of
  `available_at`) and pass it in the `BatchContext.frames` dict, alongside `minute_agg`. Backfill supplies
  the full history; live supplies the current minute's events.
- **`declare()`** the FeatureSpecs (name, ≥40-char description, dtype, `valid_range`, `nan_policy`,
  `tolerance`, layer). Use a new `FeatureType.EVENT_CLOCK` (added to the `FeatureType` enum in
  `quantlib/features/base.py`).
- **`compute()`** (backfill, source of truth): per symbol, `T - max(available_at <= T)` over the joined
  filings frame; **null when no prior filing** (most symbols, most minutes).
- **`compute_latest()`** / fold: the per-(symbol, form_type) counter described in §2, implemented as the
  new event-clock state kind in `quantlib/features/stateful.py` (`EventClockState`, guarded by
  `tests/test_fp_stateful.py`). Folds in O(symbols-with-events-this-minute).
- **Parity gates (must pass before merge):** `tests/test_fp_latest.py` (compute_latest == compute.last
  cell-for-cell), `tests/test_fp_lookahead.py` (appending future filings does not change any past value),
  `tests/test_fp_stateful.py` (fold == reseed for the new kind). Then a real-data entry in
  `docs/PARITY_COVERAGE.md`.

Because the clock value is mostly **null** (no prior filing), the `nan_policy` and the bus codec's
null handling are the load-bearing details — see Risks.

---

## 5. CONSUME — an event-driven strategy container

The bus publishes per-symbol `FeatureVector`s to Redis Streams (`fv:<SYMBOL>`, schema-fingerprinted);
strategy containers subscribe via `BusConsumer`, own their tables via `StrategyStore`, and optionally
score with a `Model.predict` (`quantlib/bus/`, `strategies/lib/`, `docs/STRATEGY_CONTAINERS.md`). The
smoke and reversion containers are the worked patterns (`strategies/smoke/strategy.py`,
`docker-compose.strategies.yml`).

Propose `strategies/event8k/` — **trade on a fresh 8-K**:
- `BusConsumer([symbols])`, poll each cycle, read `vector.value("event_clock", "minutes_since_last_8k")`.
- **Event gate:** if the clock just reset to 0 (a fresh 8-K this minute) and the value is finite, that's
  the trigger — distinct from the smoke/reversion *interval* gate; this is *event-driven*.
- `StrategyStore.from_env("event8k", [BETS_TABLE])` for the isolated `strat_event8k` schema; same risk
  caps as smoke (notional, max concurrent, hold-seconds, total-notional ceiling).
- Wire into `docker-compose.strategies.yml` copying the smoke block (env: `EVENT8K_SYMBOLS`, `…_NOTIONAL_USD`,
  `…_HOLD_SEC`, `…_MAX_CONCURRENT`). This proves the **whole spine**: a real SEC filing → `available_at` →
  event-clock feature → bus → a bet, all parity-true and look-ahead-safe.

This container is also the cleanest demonstration of an *event-driven* (vs cadence-driven) strategy shape,
which the dual-backlog edge hunt wants.

---

## 6. Port from the old Edgar repo vs build fresh

The old repo (`/home/ben/automated-day-tracking-claude`) has battle-tested EDGAR pieces. Port the **math
and the I/O glue**; leave the **app**.

### Port (reuse the logic, adapt to quant-fp's parity contract)
- **8-K item extraction** — `backend/app/fetchers/edgar/analyzer.py` (`ITEM_8K_DEFINITIONS` 2.02/5.02/7.01/…,
  sentiment tags). Feeds Phase-3 `filing_content` features.
- **Form-4 XML parsing** — same analyzer (buy/sell, net shares, value, significance threshold). Feeds
  Phase-3 insider features.
- **Event/filing feature MATH** — `backend/app/features/groups/event_features.py` and `filing_features.py`
  (form-type flags, 8-K item flags, Form-4 direction/magnitude). Port the *formulas*; re-house them in the
  quant-fp `declare()/compute()/compute_latest()` group contract (the old `compute(ctx)->float` shape is
  not parity-structured).
- **Golden-set `event_trigger` category** — `backend/app/features/quality/samples.py` (real AAPL/NVDA 8-K
  and Form-4 timestamps at 14:30 ET). The *idea* (validate event features against real filing timestamps)
  ports to quant-fp's parity-coverage audit; the samples themselves are a good seed list.

### Already ported (don't re-port — quant-fp has its own, parity-fixed)
- Atom polling, submissions backfill, CIK↔ticker mapper, token-bucket rate limiter — all reimplemented in
  `services/edgar/main.py` + `quantlib/sec_rate_limit.py`, **with the `available_at` parity fix** the old
  code lacked. The old `edgar_fetcher.py` conflates `<updated>` into `filed_at` (look-ahead leak) — do NOT
  port that timestamping.

### Build fresh (no old analogue, or old analogue is incompatible)
- The **event-clock state kind** (`EventClockState`) — the old repo computed events per-snapshot with no
  fold/reseed parity invariant; quant-fp needs the stateful abstraction.
- The **raw EDGAR parquet store + manifest** — old repo wrote straight to Postgres; the
  resumable-raw-layer discipline is a quant-fp invention.
- The **bus field wiring + event-driven strategy container** — no old analogue.

### Leave behind (Edgar-app cruft)
Frontend (React, HotTickersList, FilingsTimeline, NewsPane), HTTP API routes, watchlist, news panes,
half-life hot-score UI, the app's bespoke ML/experiment scripts. None of it is platform-shaped.

---

## 7. Phased plan

**Phase 0 — turn it on (hours).** Start the `edgar` collector so `filings` accrues live, point-in-time
`available_at` rows. Verify ingest + dedup against a short submissions backfill window (the Phase-1 parity
check already specified in `EDGAR_INGESTION.md`). Precondition for everything else.

**Phase 1 — the smallest parity-true slice end-to-end (the proof).** One feature,
`minutes_since_last_8k`, all the way to the bus:
1. `InputSpec(name="filings", …)` + the engine builds the filings frame.
2. `EventClockState` in `stateful.py` + `event_clock.py` group (this one feature).
3. Pass `test_fp_latest`, `test_fp_lookahead`, `test_fp_stateful`; add a `PARITY_COVERAGE.md` row.
4. Feature appears in the bus schema (fingerprint bump) and on `fv:<SYMBOL>`.
5. (Optional within Phase 1) the `event8k` strategy container consuming it.
**Done = a fresh 8-K moves a bus field within the bet-latency budget, backfill reproduces it cell-for-cell.**

**Phase 2 — event-clock breadth.** The rest of the clock family (`filings_today`, `is_within_30m_of_8k`,
form-type flags, `minutes_since_last_form4`). Resumable historical raw EDGAR backfill (§3b) so the clock
has deep history for the modelling agent. Point-in-time CIK↔ticker historization.

**Phase 3 — filing-content features.** Port 8-K item extraction + Form-4 parsing (§6) into a
`filing_content` group; flags + decayed magnitudes attached to the event.

**Phase 4 — fundamentals / surprise (breadth).** `filing_surprise` and fundamentals-derived features
(consensus vs reported), the larger alt-data surface. Honestly the lowest-certainty, highest-data-quality-
risk phase; sequence it last.

---

## 8. Honest risks

- **Parity for sparse event state (the #1 risk).** Most (symbol, minute) cells have no prior filing → the
  clock is **null**, and the value only changes at sparse event minutes. Null/NaN handling must be
  identical in backfill polars, the live fold, and the bus codec, or `test_fp_latest` diverges. The
  warmup/session boundary makes this worse: the clock spans non-RTH, so seeding must replay overnight
  filings (an empty warmup window silently yields wrong values). This is where the design earns or loses
  its parity claim.
- **Point-in-time correctness of `available_at`.** Live `atom_feed` time is high-confidence; deep-history
  `submissions_accepted` (acceptanceDateTime) is a flagged proxy. Mixing confidences in one feature without
  surfacing it (trust ledger / `DATA_QUALITY_LEDGER.md`) risks a silent look-ahead in the reconstructed tail.
- **CIK↔ticker drift.** Today's map applied to all history mis-maps renamed tickers — fine for the
  large-cap Phase-1 set, a real correctness bug for breadth. Must historize before Phase 2 breadth.
- **SEC rate limits / fair-access.** A universe-wide historical backfill at 4 rps is slow and SEC blocks
  bad actors (User-Agent required, already handled). The backfill is the long pole; budget and resume it
  like trades/quotes, never burst.
- **Restatements / amendments.** `8-K/A` and `10-K/A` re-disseminate; each is its own event. Treating an
  amendment as a correction-in-place would corrupt the clock. Survivorship: backfilling only today's
  universe omits delisted names — acceptable per breadth-over-depth, but must be honestly noted.
- **Data quality of content parse.** 8-K item regex and Form-4 XML are noisy (free-text items, malformed
  filings). Phase-3+ features should fail loud on parse failure (per "no lazy graceful degradation"),
  not silently emit zeros.

---

## 9. Alignment summary

| Spine stage | Market data (today) | EDGAR (proposed) |
|---|---|---|
| ACQUIRE | `raw_backfill.py` → `/store/raw/<tier>/…` + manifest; live ingestor | historical raw EDGAR parquet + manifest (§3b); live `services/edgar` (exists, turn on) |
| MATERIALIZE | `ReductionGroup`/`EMAState`/`LastKState`, parity-gated | `event_clock` group + new `EventClockState`, same parity gates (§4) |
| CONSUME | smoke / reversion strategy containers via bus | `event8k` event-driven container via bus (§5) |

EDGAR becomes a peer alt-data source on the existing spine — orthogonal signal, parity-true by
construction, no corner-cuts — rather than a bolt-on.
