# News + EDGAR data pipeline — keep-fresh / backfill / reconcile AUDIT

Operational audit of how the two **non-tape** data sources — SEC EDGAR filings and Alpaca news
articles — are captured live, backfilled, and reconciled, and the concrete gaps to close. This is the
operational counterpart to the design docs ([EDGAR_INGESTION.md](EDGAR_INGESTION.md),
[NEWS_INGESTION.md](NEWS_INGESTION.md)): those describe the intended contract; this measures what is
actually on disk/in the DB as of the audit and recommends a keep-fresh process modeled on the raw-tape
DataIntegrity workstream (manifest-driven, scheduled, monitored, memory-bounded).

**Audit timestamp:** 2026-06-21 ~01:50 UTC (Saturday night ET — both sources are in a legitimate
weekend lull, which matters for reading the freshness numbers below).

**Method:** read-only DB probes against the `filings` table (via `quant-edgar-1`'s own connection) and
read-only polars scans of `/store/news` (via the `news-capture` container). No pipeline code changed, no
container touched, no backfill run, no secrets printed.

---

## TL;DR (the load-bearing findings)

| | EDGAR filings | News articles |
|---|---|---|
| **Live service** | `quant-edgar-1` (polls SEC getcurrent Atom every 5s) | `news-capture` (Alpaca v1beta1/news websocket, 24/7) |
| **Live writes to** | Postgres `filings` table (timescaledb) | `/store/news/published_date=*/data.parquet` (fp_store_real) |
| **Newest real ingest** | **2026-06-19 01:59 UTC** (`discovered_at` max) | 2026-06-20 20:48 UTC (live), 06-19 (backfill) |
| **Coverage depth** | 1994 → 2026-06-19; **3.17M rows** | 2025-11-12 → 2026-06-20; **27.3k articles** |
| **Backfill** | submissions API, manual one-shot, ad-hoc | manifest-driven, resumable, manual one-shot |
| **Scheduled?** | **NO cron** | **NO cron** |
| **Freshness monitor / alert?** | **NONE** (and the log metric is misleading — see below) | **NONE** |
| **Reconcile discipline** | PK dedup, but **cross-seam dup risk** (30 accessions) | manifest first-sight-wins, **clean by construction** |

**Three things to act on, in priority order:**

1. **EDGAR has a monitoring blind spot that hides stalls.** `quant-edgar-1` logs `poll: 100 filings
   upserted` every 5 seconds and looks perfectly healthy — but `upsert_filings` returns the *parsed
   feed size*, not rows actually inserted, and every one of those is an `ON CONFLICT` no-op. The genuine
   newest ingest (`discovered_at`) has not advanced since 2026-06-19 01:59 UTC. **In this case it is a
   true weekend lull** (SEC publishes almost nothing Fri-night → Sun), so this is not currently a defect
   — but a *real* stall (like the fc 06-17 outage) would produce the **identical** healthy-looking log.
   That is the gap. → **First step:** add an EDGAR freshness probe (max `discovered_at` vs SEC business
   hours) to the existing monitoring, exactly as `fc` capture is watched.

2. **News live capture is producing almost nothing yet.** Only **8 live-arrival rows total**, all on
   2026-06-20. `news-capture` came up 2026-06-20 15:58 UTC and has 0 restarts, so 8 articles over ~10h
   is plausible for a quiet weekend on the `"*"` subscription — but it is too thin to be sure the feed
   is healthy, and the `news_lag` EMBARGO calibration (gated on ≥200 live rows) is nowhere close. → Let
   it accumulate over a weekday, then re-check; add the same freshness probe.

3. **Backfill is manual and un-scheduled for both**, and EDGAR has a small cross-seam dedup gap. Neither
   has a cron; both depend on someone remembering to run the seeder. The EDGAR live/backfill seam can
   double-count a filing (30 accessions today) because the two paths assign different `available_at`.

---

## 1. Keep-up-to-date (live capture)

### 1a. EDGAR filings — `quant-edgar-1`

- **Mechanism:** `services/edgar/main.py` `run_stream()` — a lightweight loop (psycopg + httpx + stdlib,
  *not* the fp-dev feature stack). Polls the SEC current-filings Atom feed
  (`browse-edgar?action=getcurrent...count=100`) every `EDGAR_POLL_SECONDS=5`, through a ~4 rps token
  bucket with the required SEC `User-Agent`. Parses each `<entry>`, maps CIK→ticker (refreshed daily
  from `company_tickers.json`), and UPSERTs.
- **Point-in-time contract (the parity crux):** `available_at` = the Atom `<updated>` dissemination
  instant, flagged `available_at_source='atom_feed'`. `filed_at`/`accepted_at` are left NULL on the live
  path (the feed carries no separate filing date). `available_at` is **never rewritten** on conflict —
  the look-ahead-safe field is immutable once seen.
- **Writes to:** Postgres `filings` table (`DB_HOST=timescaledb`, `db/init/08_filings.sql`). PK
  `(accession_number, available_at)`.
- **Config (live):** `EDGAR_MODE=stream`, `EDGAR_FORMS=*` (keep everything, filter at feature time),
  `EDGAR_POLL_SECONDS=5`, `EDGAR_SEC_MAX_RPS=4.0`.

**Measured freshness (2026-06-21 01:50 UTC):**

- `filings` total: **3,175,782** rows; 99.9% have a non-null symbol; 5,628 distinct symbols.
- Newest `available_at`: **2026-06-19 01:59:08 UTC**. Newest **`discovered_at`** (true ingest time):
  **2026-06-19 01:59:40 UTC** → ~1d 23h "stale".
- Stream-path (`source='stream'`) rows: **8,132**, spanning 2026-06-16 14:42 → 2026-06-19 01:59. **Zero
  stream rows ingested since 06-19 02:00**, including zero after the 06-20 08:48 restart.
- Filings-per-day, recent: 06-15 (Mon) 1558, 06-16 2684, 06-17 2655, 06-18 3116, **06-19 (Fri) 56**,
  06-20/21 (weekend) 0.

**Reading the freshness:** 06-19 is a Friday; its 56 filings all landed in the early-UTC hours and the
last at 01:59 UTC (~10 PM ET Thu / overlapping SEC's nightly EDGAR maintenance window and the Fri-night
→ weekend lull when SEC publishes almost nothing). So the ~2-day "staleness" is **expected weekend
behavior, not a confirmed outage.** The container restarted 06-20 08:48 UTC (RestartCount=10 over its
life) and is actively polling now.

**The monitoring blind spot (key gap):** the log line `poll: 100 filings upserted` is emitted whenever
the parsed feed is non-empty, because `upsert_filings` returns `len(filings)` (feed size), not the count
of genuinely-new rows. With `count=100` in the feed URL, a steady-state poll *always* reports "100
upserted" even when all 100 are `ON CONFLICT` no-ops. **There is no signal that distinguishes "captured
100 new filings" from "re-saw the same 100, wrote nothing."** A genuine stall (feed frozen, DB write
failing silently, account/IP block) would log identically to health. Nothing watches max `discovered_at`
and alerts — unlike the fc capture, which has `live_monitor`.

### 1b. News articles — `news-capture`

- **Mechanism:** `quantlib/features/news_capture.py` `run_news_capture()` — owns Alpaca's
  `NewsDataStream` (`v1beta1/news`) websocket, 24/7 (not market-hours gated), subscription `"*"` (all
  symbols, `FP_NEWS_SYMBOLS=*`). Buffers articles and flushes micro-batches (≥25 articles **or** ≥30s)
  into the store. SDK auto-reconnects.
- **Point-in-time contract:** each live article's `available_at` = the **websocket arrival instant**
  (when WE saw it), flagged `available_at_source='live_arrival'` — never look-ahead. `published_at`
  keeps Alpaca's `created_at` as honest metadata.
- **Writes to:** `/store/news/published_date=<UTC-date>/data.parquet` on the `fp_store_real` volume,
  via `news_store.upsert_articles` (de-dup by article `id`, first-sight wins). Manifest part appended
  per touched date.
- **Isolation:** binds `/home/ben/quant-fp-news` as `/app` (a separate tree — **not** the live fc
  `/home/ben/quant-fp`), computes NO features, never touches fc/its fingerprint/bus.

**Measured freshness (2026-06-21 01:50 UTC):**

- 187 date partitions, 27,254 total articles, span 2025-11-12 → 2026-06-20.
- Newest article `available_at` (any source): 2026-06-20 20:48:51 UTC (~5h ago).
- **Live rows: only 8 total**, all on 2026-06-20, `available_at` 17:01 → 20:48 UTC.
- `news-capture` started 2026-06-20 15:58 UTC, **RestartCount=0**.

**Reading the freshness:** the container is up and writing (8 live rows confirm the path works end to
end), but the volume is tiny. On a Saturday-night weekend lull on the full universe this is *plausible*,
but 8 articles is too few to confirm feed health or to calibrate `news_lag` (which needs ≥200 live rows
for a stable p90 EMBARGO). No monitor/alert exists here either.

---

## 2. Backfill

### 2a. EDGAR backfill

- **Mechanism:** `services/edgar/main.py` `run_backfill()` (selected by `EDGAR_MODE=backfill` +
  `EDGAR_BACKFILL_SYMBOLS`). Walks the SEC submissions API
  (`data.sec.gov/submissions/CIK{cik}.json`) per symbol. Historical `available_at` =
  `acceptanceDateTime`, flagged `available_at_source='submissions_accepted'` (explicitly lower
  confidence than the live atom instant — see the design doc's parity note). `filed_at` = company
  `filingDate` (metadata).
- **Depth on disk:** `submissions_accepted`/`backfill` rows = **3,167,650** (99.7% of the table). The
  `available_at` floor is **1994-01-07** — i.e. essentially the full SEC submissions history for the
  seeded symbols is present.
- **Manifest-driven? NO.** Unlike the raw tape and the news store, EDGAR backfill has **no manifest /
  no resume key**. It relies entirely on the `filings` PK + `ON CONFLICT` to avoid re-inserting, so a
  re-run is idempotent at the row level but **re-fetches every symbol's full submissions JSON every
  time** (no "already-done" skip). It is a one-shot CLI: pass a symbol list, it pulls, it exits.
- **Scheduled? NO cron.** Manual invocation only.
- **Gaps:** the submissions `recent` block only returns each company's most recent ~1000 filings; older
  history requires the paginated `submissions/CIK...-submissions-NNN.json` files, which this backfill
  **does not fetch**. So for high-frequency filers, the deep tail before ~1000 filings ago is absent.
  Coverage is also limited to whatever symbol list was last passed (5,628 symbols have filings today).

### 2b. News backfill

- **Mechanism:** `quantlib/data/news_backfill.py` — bounded, memory-safe, **resumable** seed of
  `/store/news`. Pulls Alpaca v1beta1/news history per symbol-chunk per UTC day for a trailing `--days`
  window (default 30), `--top N` liquid universe symbols (default 100) or `--symbols`. Single-process by
  design (`--processes>1` is rejected). Refuses to write below 5 GiB free. **Must be named
  `quant-backfill*`** so `live_monitor`'s memguard protects it.
- **Depth on disk:** 186 backfill-seeded dates, 2025-11-12 → 2026-06-19.
- **Manifest-driven? YES** — `news_store.backfilled_dates()` reads `source='alpaca_created'` manifest
  parts as the resume key, so a re-run skips already-seeded dates. Empty (no-news) days get a 0-article
  manifest part so they are marked done, not re-fetched. This is the raw-tape discipline, correctly
  applied.
- **Scheduled? NO cron.** Manual one-shot.
- **Gaps (measured):** within the 2025-11-12 → 2026-06-20 span (221 calendar days), **187 present, 34
  missing**:
  - **29-day contiguous early gap Nov 13 → Dec 11 2025** — the un-seeded tail. The last seed ran with a
    `--days` window reaching ~Nov 12 and stopped; everything before is simply not yet pulled. This is the
    real coverage hole.
  - **5 later scattered missing dates** — 2025-12-13/14 (Sat/Sun), 12-20/21 (Sat/Sun) are legitimate
    empty weekend days; **2025-12-15 (Monday) is a genuine 1-day hole** worth a targeted re-seed.

---

## 3. Reconcile (live ↔ backfill seam)

### 3a. EDGAR — small cross-seam double-count risk

The `filings` PK is `(accession_number, available_at)`. The live path writes `available_at` = atom
`<updated>`; the backfill path writes `available_at` = submissions `acceptanceDateTime`. **These differ
for the same filing**, so the same accession can land as **two rows** under two different sources — the
PK does *not* dedupe across the seam.

Measured: **30 accessions exist in BOTH `stream` and `backfill`**, with their two `available_at` values
~4.5h apart on average (max 5h). (A separate ~1,700 within-backfill dups are amendments/refiles of the
same accession, which is legitimate.) For the filing-frequency features this is a **real but small
double-count risk**: if both `available_at` values fall inside the same trailing-count window for a
symbol, that filing is counted twice. Today the blast radius is 30 accessions, but it grows every time
backfill is re-run over dates the live feed already covered. This is the one place EDGAR lacks the
raw-tape's clean manifest seam.

### 3b. News — clean by construction

The news store dedupes by article **`id`** (first-sight wins, `ingested_at` ascending), independent of
source. An id seen by both live and backfill is written **once**, and its `available_at` is then
immutable. So a (symbol, minute) hotness count gated on `available_at <= minute` yields the identical
article set live vs backfill — **no cross-seam dup, no seam gap**. This matches the raw-tape
manifest discipline. (The honest caveat, already documented in NEWS_INGESTION.md: which path wins
fixes whether `available_at` is the arrival instant or `created_at`; the Modeller's frozen
availability-lag EMBARGO is what makes the feature robust to that difference.)

---

## 4. Dependent features (what trusts/breaks if this data is stale)

- **EDGAR → `edgar_filing_frequency` group** (`quantlib/features/groups/edgar_filing_frequency.py`,
  registered, fingerprint-affecting). Features: `edgar_filing_count_{window}d`,
  `edgar_minutes_since_last_filing`, `edgar_minutes_since_last_8k`, `edgar_filing_count_<form>` (per
  form type, trailing 90d), `edgar_filing_burst` (7d vs 365d baseline rate). All read the `filings`
  table point-in-time (`available_at <= minute`) over a `[day_start-90d, day_end)` snapshot.
  - **If the EDGAR stream stalls:** these features silently go stale — `minutes_since_last_filing`
    keeps counting up, counts undershoot for any symbol that filed during the stall, and `filing_burst`
    misreads. Because the stall is invisible in the logs (finding #1), the features would be wrong with
    no alarm. This is the concrete reason the freshness monitor matters.
  - **The cross-seam dup (3a)** can inflate `edgar_filing_count_*` / `edgar_filing_burst` for the ~30
    affected accessions.
- **News → hotness features: NOT YET BUILT.** No news feature group is registered
  (`news_hotness`/`news_count`/`news_intensity` absent from `quantlib/features/`). The hotness hunt is
  pre-registered (`experiments/2026-06-20-news-hotness/prereg.md`) and gated on the `news_lag` EMBARGO,
  which itself is gated on ≥200 live rows (we have 8). So today **nothing in the fingerprint depends on
  the news tape** — staleness here has no trust impact yet, but it blocks the hunt from starting.

---

## 5. Gaps + recommended keep-fresh / backfill process

### Concrete gaps

| # | Gap | Severity |
|---|---|---|
| G1 | **No EDGAR freshness monitor**; the `100 upserted` log can't distinguish a stall from health | **High** — silent wrong features |
| G2 | **No news freshness monitor** | Medium (no live dependents yet) |
| G3 | **No scheduled backfill** for either source (both manual one-shots) | Medium — coverage rots |
| G4 | **EDGAR cross-seam dedup gap** — 30 accessions double-rowed (live vs backfill `available_at`) | Medium — count inflation |
| G5 | **EDGAR backfill is not manifest-driven** — re-fetches everything, no resume, no deep-tail (>~1000 filings ago) pages | Low/Medium |
| G6 | **News coverage hole** — 29-day un-seeded tail (Nov 13 → Dec 11 2025) + 1 genuine day (2025-12-15) | Low — extend `--days` |
| G7 | **Dead orphan container `python-news-container`** (Exited 1, image `custom_delta_image`, since 2026-01-04) | Cleanup — flag for Lead |

### Proposed process (modeled on the raw-tape DataIntegrity workstream)

1. **Freshness monitoring (G1, G2) — do this FIRST.** Add an EDGAR + news freshness probe to the
   existing monitoring loop, parallel to how `fc` capture is watched. EDGAR: alert when max
   `discovered_at` lags SEC *business hours* (Mon-Fri ~06:00-22:00 ET) by more than a small threshold
   (e.g. 30-60 min) — keyed off `discovered_at`, NOT the log line, and weekend/maintenance-window-aware
   so it does not page on legitimate lulls. News: alert when max live `available_at` lags wall-clock by
   more than a threshold during active hours. Surface both on the dashboard alongside raw-tape coverage.
2. **Fix the misleading EDGAR metric (G1).** Make `upsert_filings` / the poll log report *rows actually
   inserted* (new accessions), not feed size — so "0 new for N hours" is visible. (Code change; out of
   scope for this audit, flagged for the Lead.)
3. **Scheduled, manifest-driven backfill (G3, G5, G6).** Register an idempotent, memory-bounded,
   RTH-safe nightly seed in `docs/OPERATIONS.md`'s cron registry:
   - News: nightly `news_backfill --days <covers the gap>` (resumable manifest already skips done dates;
     extend `--days` once to backfill Nov 13 → Dec 11 2025 + 2025-12-15).
   - EDGAR: give the submissions backfill a **manifest/resume key** (per-CIK last-seen) so a nightly
     top-up doesn't re-pull every symbol, then schedule it for the active symbol universe.
4. **Close the EDGAR seam (G4).** Either (a) reconcile dedup on `accession_number` alone at feature
   read-time (prefer the `atom_feed` row's `available_at` when both exist), or (b) on backfill, skip
   accessions the live feed already captured. Prevents count inflation as backfill re-runs.
5. **Cleanup (G7):** flag `python-news-container` (Exited 5 months) for the Lead to `docker rm` — this
   audit does not touch containers.

**First concrete step:** wire the **EDGAR freshness probe** (max `discovered_at` vs SEC business hours,
weekend/maintenance-aware) into the monitoring + dashboard. It closes the highest-severity gap (a silent
stall that corrupts the trusted `edgar_filing_frequency` features) and is the same pattern already
proven for fc capture.

---

## Appendix — probe commands (read-only, reproducible)

EDGAR (run inside `quant-edgar-1`, which already has DB creds in env):

```
docker exec quant-edgar-1 python -c "import os,psycopg; \
c=psycopg.connect(host=os.environ['DB_HOST'],port=5432,dbname=os.environ['DB_NAME'],\
user=os.environ['DB_USER'],password=os.environ['DB_PASSWORD']).cursor(); \
c.execute(\"SELECT max(discovered_at), max(available_at) FROM filings WHERE source='stream'\"); \
print(c.fetchone())"
```

News (run inside `news-capture`, which mounts `/store` + has polars):

```
docker exec news-capture python -c "import glob,os; import polars as pl; \
from quantlib.data.news_store import SRC_LIVE; \
f=pl.read_parquet(sorted(glob.glob('/store/news/published_date=*/data.parquet'))); \
print('articles',f.height,'live',f.filter(pl.col('available_at_source')==SRC_LIVE).height, \
'max_avail',f['available_at'].max())"
```
