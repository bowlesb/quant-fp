# News Ingestion — Alpaca v1beta1/news → raw `/store/news/` tape

The gating data dependency for the **news/event edge axis** (Ben's steer after 9 price/microstructure
nulls). This is RAW ACQUISITION + a new store — NOT a feature. News-hotness FEATURES come later via the
Modeller's pre-registered hunt as a separate, fingerprint-affecting PR. Nothing here touches the equity
feature-computer, its bus, or its fingerprint.

Mirrors two existing patterns: the **crypto-capture** 24/7 detached-container ingestion service and the
**EDGAR filing-frequency** point-in-time parity contract (`available_at <= minute`, fixed-at-first-sight).

## Components

| File | Role |
|------|------|
| `quantlib/data/news_store.py` | Pure storage: append-only, manifest-tracked, date-partitioned article tape + the parity contract. **(SCHEMA — Lead review.)** |
| `quantlib/data/news_fetchers.py` | Alpaca historical `NewsClient` (v1beta1/news) paginated fetch + row normalization. |
| `quantlib/data/news_backfill.py` | Bounded, memory-safe, resumable seed CLI (`--processes 1`, `quant-backfill*` named). |
| `quantlib/features/news_capture.py` | 24/7 live `NewsDataStream` ingestion (detached, restartable, idempotent). |
| `docker-compose.news.yml` | The `news-capture` container (fp-dev image, `fp_store_real` volume). |
| `tests/test_fp_news_store.py`, `tests/test_fp_news_capture.py` | 15 tests (parity contract, de-dup, row normalization, CLI guards). |

## Store layout

```
<store>/news/published_date=<YYYY-MM-DD>/data.parquet   # one parquet per UTC publish-date
<store>/news/_manifest.d/part-*.parquet                 # append-only manifest parts (unioned on load)
```

**Partitioned by DATE only** (not `symbol=/date=` like the raw bar tape): a single Alpaca article carries
a `symbols` LIST (multi-symbol co-mention), so per-symbol partitioning would duplicate every multi-symbol
article N times. We store ONE row per article, keyed by `id`, with `symbols` as a list column. Hotness
features EXPLODE the list at read time — cheap, article stored once. (Verified live: a 12-symbol
AMD/AVGO/.../NVDA co-mention is one row.)

### Schema (per-article row) — **needs Lead approval**

| Column | Type | Meaning |
|--------|------|---------|
| `id` | Int64 | Alpaca article id — the de-dup key |
| `symbols` | List[String] | every symbol the article mentions |
| `available_at` | Datetime(UTC) | **point-in-time gate** — the look-ahead-safe field features key on |
| `available_at_source` | String | `live_arrival` (websocket arrival) or `alpaca_created` (backfill publish instant) |
| `published_at` | Datetime(UTC) | Alpaca `created_at` — honest publish-instant metadata |
| `updated_at` | Datetime(UTC) | Alpaca `updated_at` |
| `headline`, `summary`, `source`, `author`, `url` | String | article content/metadata (no full HTML body — hotness is count/intensity, not content parsing) |
| `ingested_at` | Datetime(UTC) | when WE wrote it (first-sight ordering key) |

## The parity contract (why backfill == live by construction)

1. `available_at <= minute` is the only point-in-time gate a hotness feature uses.
2. `available_at` is **FIXED AT FIRST SIGHT**: the store de-dups by `id`, earliest `ingested_at` wins, so
   an id written once keeps its original `available_at` forever (a later re-fetch never overwrites it —
   proven by `test_dedup_by_id_first_sight_wins`).
3. Therefore the gated article SET at any minute T is identical in live and backfill → parity-true by
   construction, exactly the EDGAR filing-frequency contract.

**`available_at` semantics / the feed-delay seam (the one Lead-decision-relevant nuance):**
- **Live** sets `available_at` = websocket ARRIVAL instant (when WE saw it) — never look-ahead.
- **Backfill** sets `available_at` = Alpaca `created_at` (the article publish instant).
- These differ by the live feed delay. This is intentional and handled DOWNSTREAM: the Modeller's hunt
  applies an explicit, frozen-pre-data availability-lag offset on the reader side (Ben's "robust to feed
  delay" insight). The store keeps honest provenance via `available_at_source` so the reader can choose.
  Once an id is seen by EITHER path it is parity-stable. **Open question for Lead:** whether to normalize
  live `available_at` to `created_at + fixed_lag` at the STORE layer instead of the reader. Current design
  keeps the store honest (raw arrival) and pushes the lag model to the (frozen) feature — recommended, but
  flagged.

## Pilot seed — RAN (proof of pipeline)

```
docker run --rm --name quant-backfill-news-pilot --env-file .env \
  -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \
  python -m quantlib.data.news_backfill --store /store --days 3 \
  --symbols AAPL,MSFT,NVDA,TSLA,AMZN,SPY,QQQ,GOOGL,META,AMD --processes 1
```

Result: 116 articles across 4 published-date partitions (06-13/17/18/19) on `fp_store_real`. Re-run =
0 new (idempotent resume + id de-dup). `--processes 2` correctly rejected.

## Full-seed plan (NOT run — bounded, staged, Lead/memguard-gated)

The hunt needs a deeper, broader liquid-core history. Staged so it stays memory-safe and never starves
live capture:

1. **Universe:** top-1000–1500 liquid names (`--top 1500`, or pass the deep-quote-panel liquid list).
   News-per-day is light (~tens-hundreds of articles/day for the liquid core), so the bottleneck is
   Alpaca API pages, not RAM/disk — but keep `--processes 1` and name it `quant-backfill-news` for the
   live_monitor memguard.
2. **Depth:** `--days 180` (6 months) to match the order-flow/strategy panel windows. Resumable per UTC
   date, so it can run in chunks and pick up after any interruption.
3. **Disk:** news is tiny vs the tick tape (115 articles ≈ a few KB); 6mo×1500 names is well under 1 GB.
   The seeder still refuses to write below 5 GiB free.
4. **Ongoing capture:** once the live `news-capture` container is up (`docker-compose.news.yml`), it grows
   today forward 24/7; the backfill only seeds history once. The two never double-count (id de-dup).

Recommended first full-seed command (Lead to fire when ready):
```
docker run -d --name quant-backfill-news --env-file .env \
  -v fp_store_real:/store -v "$PWD":/app -w /app fp-dev \
  python -m quantlib.data.news_backfill --store /store --days 180 --top 1500 --processes 1
```

## Boundaries respected

- No fingerprint change (raw acquisition + new store, no feature registered). Live equity fc untouched.
- Worktree → PR off origin/main; schema proposed for Lead review, not self-merged.
- GOLDEN RULE honored (fc never touched); memory-bounded seed; secrets read from env, never printed.
