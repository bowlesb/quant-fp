# EDGAR filings — ingestion + the event-clock feature kind (design)

> Bring SEC EDGAR filings into quant-fp as an alt-data source. Start by COLLECTING filings in real-time
> (Phase 1); the features (minutes-since-8K, etc.) come after. Status: DESIGN (2026-06-14). Reuses the
> prior project's EDGAR code (inventoried) and FIXES its one critical parity gap. Pairs with
> FEATURE_STATE_GUIDE.md (a new "event-clock" state kind) and the breadth/alt-data edge direction.

## What's reusable from the old project (don't rebuild)
The `automated-day-tracking` repo already has solid pieces — port them:
- **Token-bucket SEC rate limiter** (`token_bucket.py`, ~4 rps, Prometheus-instrumented) — SEC fair-access.
- **CIK↔ticker mapper** (`cik_mapper.py`) — pulls SEC `company_tickers.json`, caches + refreshes daily.
- **Atom-feed polling** (`browse-edgar?action=getcurrent...output=atom`, ~5s) for real-time discovery.
- **SEC submissions API backfill** (`data.sec.gov/submissions/CIK{cik}.json`) for history.
- **8-K item regex + Form-4 XML parsing** (for the later feature phase).
- **Required**: a real `User-Agent` header on every SEC request (they block without it).

## The one thing the old code got WRONG (the parity crux)
The old schema stores `filed_at` and conflates it with the Atom `<updated>` feed time. For a parity-true,
look-ahead-safe feature we MUST separate **three** timestamps and key features off the right one:

| field | meaning | use |
|---|---|---|
| `filed_at` | the company's filing date (often a date, not a time) | metadata only |
| `accepted_at` | SEC acceptance datetime | metadata |
| **`available_at`** | when the filing became **publicly visible** (the feed `<updated>` / dissemination time) | **THE point-in-time field features key off** |

A feature like "minutes since last 8-K" MUST use `available_at` — the moment a real-time consumer could
have known. Using `filed_at` leaks look-ahead. **Backfill must replay the EXACT `available_at` we
recorded live** (or, for deep history before we collected, the best reconstructable dissemination time,
explicitly flagged as lower-confidence). This is the EDGAR analogue of our intraday point-in-time rule.

## Phase 1 — just collect (the starting feature the owner asked for)
A standalone ingestor service (mirrors the platform's other ingestors): poll the EDGAR current-filings
Atom feed every ~5s, dedupe by accession, map CIK→ticker, and write each filing to a `filings`
hypertable — capturing `available_at` at the moment we see it. No features yet; just a clean, growing,
point-in-time-correct filing store.

```sql
CREATE TABLE filings (
  accession_number text PRIMARY KEY,
  cik              text NOT NULL,
  symbol           text,                 -- mapped via cik_mapper; NULL if unmapped (kept, not dropped)
  form_type        text NOT NULL,        -- '8-K','10-K','4',...
  filed_at         timestamptz,          -- company filing date (metadata)
  accepted_at      timestamptz,          -- SEC acceptance (metadata)
  available_at     timestamptz NOT NULL, -- WHEN WE SAW IT public — the point-in-time field (look-ahead-safe)
  link             text,
  discovered_at    timestamptz NOT NULL DEFAULT now()  -- our wall-clock receipt (ops/debug)
);
SELECT create_hypertable('filings','available_at', chunk_time_interval => INTERVAL '7 days');
```
Parity for Phase 1 = a coverage/timestamp check: the filings we collected live match what the SEC
submissions backfill reports for the same window, and `available_at` is preserved.

## Phase 2 — the event-clock state kind (features)
Filings are sparse + event-driven, so the natural features are an **event clock**, a NEW `FeatureState`
kind (FEATURE_STATE_GUIDE.md): per (symbol, event-type), `minutes_since_last_8k`, `filings_today`,
`is_within_30m_of_8k`, form-type flags. Folds trivially and is parity-true by `fold==reseed`:
- **live fold:** each minute, increment the since-counter; on a filing event (an `available_at` in this
  minute), reset to 0. O(symbols-with-events).
- **backfill:** the same value is `T − max(available_at <= T)` from the stored filing timestamps —
  identical by construction, because both read `available_at`.
Spans non-market-hours (a 2am 8-K affects the morning), so the clock runs across the session/warmup
boundary — distinct from the intraday RTH-scoped features.

## Phase 3 — filing-content features
Port the 8-K item extraction (has_earnings/guidance/officer-change/...) and Form-4 insider features.
These attach to the event and decay (the old project's half-life-60min hot-ticker score is a good model).

## Parity issues to carry through
1. **`available_at` is the contract** (above) — the whole look-ahead story.
2. **CIK↔ticker mapping is point-in-time** — companies change tickers; map filings to the ticker valid at
   `available_at`, and historize the mapping (same class as `sector_map`/reference historization).
3. **Amendments** (10-K/A) re-disseminate — each amendment is its OWN event with its OWN `available_at`;
   never backfill the amendment's time onto the original.
4. **Unmapped CIKs** (no ticker) are kept with `symbol=NULL`, never dropped (mapping may resolve later).
5. **Dedup** on accession across the live feed + backfill (the old code does this — keep it).

## Phase 1 — AS BUILT (2026-06-14, feature/edgar-collect)
Collection is implemented; no features. What shipped:
- **`db/init/08_filings.sql`** — the `filings` hypertable. The three timestamps are `filed_at`
  (metadata), `accepted_at` (metadata), `available_at` (NOT NULL, point-in-time). TimescaleDB forces
  the partition column into any PK, so the PK is `(accession_number, available_at)` — still effectively
  accession-keyed because `available_at` is fixed at first sight and never rewritten. Plus
  `available_at_source` ('atom_feed' live / 'submissions_accepted' backfill) to flag confidence, and
  indexes on `(symbol, available_at)`, `(cik, available_at)`, `(form_type, available_at)`.
- **`services/edgar/main.py`** — lightweight ingestor (httpx + psycopg + stdlib, mirrors
  `services/scheduler`). Stream mode polls the Atom feed every ~5s, parses entries to filing dicts,
  maps CIK→ticker (in-memory map refreshed daily from `company_tickers.json`), and UPSERTs with
  `available_at = <updated>`. Backfill mode walks the submissions API for a symbol list. Dedup +
  late-mapping fill via `ON CONFLICT ... DO UPDATE SET symbol = COALESCE(...)` (never rewrites
  `available_at` or downgrades `available_at_source`).
- **`quantlib/sec_rate_limit.py`** — synchronous token bucket (~4 rps) shared by stream + backfill.
- **The parity fix realized:** the live feed sets `available_at` only; `filed_at`/`accepted_at` stay
  NULL on the stream path (the old code copied `<updated>` into `filed_at` — that conflation is gone).
- **Tests:** `tests/test_edgar_ingest.py` — Atom-entry → 3-timestamp dict, CIK mapping, unmapped-CIK
  kept, form filter, submissions backfill confidence flag. Network/DB-free.

Honest remaining work (NOT done here): deep-history `available_at` confidence (acceptanceDateTime is a
proxy, flagged lower-confidence); point-in-time CIK↔ticker historization (today's map is applied to all,
which can mis-map a renamed ticker); the Phase-2 event-clock feature kind.

## Why this is worth it
Alt-data (filings) is exactly the breadth-over-depth, edge-hunt direction — it's orthogonal signal a
modelling agent can't get from price/volume alone, and the event-clock kind is a clean, parity-true
addition to the platform. Phase 1 (collect) is low-risk and starts the data accruing immediately.
