# NEWS HOTNESS / INTENSITY — pre-registration (frozen before the data exists)

**Author:** Modeller · **Date:** 2026-06-20 · **Status:** PRE-REGISTERED DESIGN — written BEFORE the news
store exists (the strongest possible discipline: the feature defs, labels, baselines, and median-anchored
kill conditions are frozen now; DataIntegrity is building the gating Alpaca-news ingestion + `news_articles`
store in parallel). The Lead gate-reads this BEFORE any run, same as LP. Data-gated; the pre-reg quality is
the deliverable.

## WHY THIS AXIS — the one surface orthogonal to all 9 nulls

9 settled negatives, ALL on price / microstructure (cross-sectional direction ×4 framings, EDGAR-frequency,
8-K event, weekly reversal, monthly low-turnover, liquidity provision). News is the last signal source
genuinely ORTHOGONAL to every one of them — it is information ARRIVAL, not a function of the price tape. And
Ben's framing is the reason it can work where the prior surfaces couldn't: **HOTNESS / INTENSITY over a
trailing window (counts/coverage-density), NOT a point-in-time latency-sensitive headline reaction.** A
count-over-a-window is robust to feed delay by construction — if an article lands a few minutes late, the
trailing-window count is barely perturbed, whereas a headline-instant trade is destroyed by the same delay.
This robustness is the whole thesis.

## ⚠️ PLATFORM BAR — RT-trivial + parity-true BY CONSTRUCTION (non-negotiable)

Every feature here is a COUNT / RECENCY / DECAY-WEIGHTED-SUM over articles with a look-ahead-safe
`available_at <= ctx.timestamp` — exactly the mechanism of the EDGAR filing-frequency features. This is
RT-trivial (a windowed count from the store) and parity-true by construction (deterministic from the
`news_articles` store + `ctx.timestamp`; identical live and in backfill since `available_at` is fixed at
first-sight — the same compute-time-join parity contract as `sector_map`/`corporate_actions`/`filings`). NO
point-in-time headline NLP, NO embeddings, NO FinBERT on the fast path (the old repo's embeddings/finbert
tiers are explicitly OUT — they're heavy + a different parity story). Counts + a SIMPLE keyword/relevance
flag only.

## DATA CONTRACT (what the design assumes DI's store provides — pre-committed)
`news_articles(id, available_at, symbols[], headline, source, ...)` with:
- `available_at` = the look-ahead-safe first-sight dissemination instant (the SAME discipline as `filings`),
  microsecond. THIS is the only timestamp features may read (NOT a `published_at` that could be revised).
- `symbols[]` = the tickers tagged to the article (for per-name attribution + the exclusivity/dilution
  relevance refinement).
- `headline` (for a simple regex headline-mention + clickbait/market-wide flag — keyword tables ported from
  the old repo's relevance.py, NO ML).
If the store ships without `symbols[]` or a reliable `available_at`, this design does NOT run — flagged to DI.

## FEATURE DEFINITIONS (frozen — windows, count vs decay, per-name normalization)

### A. RAW HOTNESS (counts/recency over trailing windows; the core intensity tier)
For each name at `ctx.timestamp = T`, over articles with `available_at + EMBARGO <= T` tagged to the name
(EMBARGO = the feed-delay buffer, §feed-delay below):
- `news_count_{5m,15m,1h,4h,24h,7d}` = # articles in the trailing window.
- `news_recency_min` = minutes since the most recent article (capped at a large constant when none).
- `news_velocity` = articles/hour over the trailing 24h.
- `news_has_recent` = 1 if ≥1 article in the trailing 15m (the "is it live" binary), else 0.

### B. PER-NAME-NORMALIZED HOTNESS (the abnormality — a small-cap's "5 articles" ≠ a mega-cap's)
A raw count is incomparable across names; the SIGNAL is the abnormality vs the name's OWN baseline.
Pre-committed, point-in-time (NO full-sample baseline):
- `news_hot_z_{1h,24h}` = (`news_count_W` − μ_name) / σ_name, where μ_name, σ_name are the name's trailing
  baseline of that window's count over the prior `BASELINE_DAYS = 60` calendar days, computed STRICTLY from
  data with `available_at < T − W` (no overlap with the current window → no look-ahead). A name with too few
  baseline observations → NULL (no faked z).
- `news_burst_ratio_24h` = `news_count_24h` / (trailing-60d mean daily count + 1) — the EDGAR-burst shape,
  per-name (so it's coverage RELATIVE to the name's normal, the only cross-name-comparable hotness).

### C. RELEVANCE-REFINED HOTNESS (is the coverage ABOUT this name, or tangential — ported, keyword-only)
From `symbols[]` + `headline` regex (old-repo relevance.py keyword tables, NO ML):
- `news_count_exclusive_24h` = # trailing-24h articles where this ticker is the ONLY tagged symbol (pure
  single-name coverage, the cleanest hotness).
- `news_count_headline_24h` = # where the ticker (name/symbol) appears in the headline.
- `news_dilution_avg_24h` = mean # of OTHER tagged symbols per trailing-24h article (high = market-wide
  noise, low = name-specific) — a relevance DOWN-weighter, reported as a conditioner.

(Sentiment is DELIBERATELY EXCLUDED from this pre-reg — it's a different parity/ML story; this hunt is pure
intensity, Ben's framing. A sentiment tier would be a separate pre-reg if the intensity tier shows signal.)

## FEED-DELAY MODEL (pre-committed — ask #4, the load-bearing robustness)
- `EMBARGO` (frozen `= 5 min`): a feature at `T` only counts articles with `available_at + 5min <= T`. This
  is the conservative actionability lag (the EDGAR re-test value) — if DI's `available_at` is the
  ingestion-sight instant, a real article may have been public slightly before; the 5-min embargo ensures we
  never count an article we couldn't have acted on, AND it is the explicit feed-delay buffer.
- ROBUSTNESS-TO-DELAY is the design's reason to exist, so it is TESTED: re-run every result under EMBARGO ∈
  {1, 5, 15} min. A real hotness edge must be STABLE across the embargo (a count over a 1h/24h window barely
  moves if articles are 5-15 min late). A signal that exists only at EMBARGO=1 (no delay) and dies at
  EMBARGO=15 is NOT delay-robust → reported as fragile/non-tradeable. This embargo-stability test is the
  operationalization of Ben's "robust to feed delay."

## HYPOTHESES (pre-registered — 2)

### H1 — HOTNESS → forward VOLATILITY / move-magnitude (primary; the intensity prior)
**Claim:** an abnormal coverage spike (`news_hot_z`, `news_burst_ratio`) predicts elevated forward realized
move-magnitude / volume — the information-arrival intensity effect. This is the magnitude axis that survived
weakly in EDGAR (#187) and is the natural home for an intensity signal. Targets: forward |return|, forward
realized vol, forward volume over {30m, EOD, next-day}.

### H2 — HOTNESS → forward DIRECTION (secondary; skeptical prior, tested honestly)
**Claim:** abnormal coverage predicts signed forward return (over/under-reaction drift). Prior: SKEPTICAL —
direction has nulled 9×. Tested both as a raw signed-return predictor AND conditioned on the relevance tier
(exclusive/headline coverage may carry more directional information than diluted market-wide mentions). In
the FDR family; a favorable mean can't reopen it (median gate).

## DISCIPLINE (the full spine — the same that made the 9 nulls trustworthy)
- **Tradeable entry ≥09:35 ET**, forward labels from the tradeable entry (off-hours news → next-session
  open, the #197 handling); never a same-instant headline print.
- **Per-name point-in-time baselines** (§B) — no full-sample normalization, no look-ahead in μ/σ.
- **Predict-zero AND shuffle baselines:** shuffle = permute the article→name attribution within a timestamp
  (break the name-specific hotness link); a real edge survives, a coverage-wide artifact vanishes.
- **Walk-forward with PURGE:** train/early vs test/late split with a purge gap ≥ the longest label horizon
  (no leakage across the boundary); report OOS sign-consistency.
- **Own-vol / size / liquidity control** (the #187/#205/#212 lesson): partial out trailing vol + log-ADV +
  (for hotness) the name's baseline coverage level, so we measure NET-NEW intensity, not "big liquid names
  get more news AND move more." Collapse ratio reported.
- **NET-OF-REAL-COST MEDIAN gate:** the verdict stat = net forward P&L MEDIAN after realistic cost (the real
  effective spread from the now-queryable quote tape on the overlap window, else 5/10bps). MEDIAN-anchored:
  a favorable mean CANNOT reopen it (the gate that caught every trap).
- **$1-floor + per-period winsor**; **EMBARGO-stability** test (§feed-delay).
- **BY-FDR** across all (hypothesis × window × target × embargo) cells.

## KILL CONDITIONS (median-anchored, stated in advance)
- **SETTLES NULL** iff the net forward MEDIAN ≤ 0 on the verdict setting REGARDLESS of mean, OR the hotness→
  magnitude relation COLLAPSES under the own-vol/size control (it was "big liquid names" not news), OR it is
  not EMBARGO-stable (a delay artifact).
- **REOPENS / promotable** iff net MEDIAN > 0 AND survives shuffle + own-vol/size control (collapse stays
  high) AND EMBARGO-stable AND OOS-consistent under the purge → the FIRST orthogonal-axis edge → FLAG the
  Lead for confirmatory replication BEFORE excitement.
- **NON-ROBUST** (reported, not promoted) iff a positive median appears only at one embargo / one window /
  one OOS half / before the own-vol control.

## RUN PLAN (data-gated; design is this cycle's deliverable)
Build (`build_news_panel.py`) joins the `news_articles` store to the deep bar/quote panel at compute time
(the same parity-true compute-time join as `filings`/`sector_map`), emits the per-(name, entry) hotness
panel; screen (`screen.py`) = the median-anchored net-of-cost gate + shuffle + purge-OOS + own-vol control +
embargo-stability + FDR. Reuses the #205/#212/#226 host-mounted resumable cache + chunked-subprocess infra.
Build starts only after (a) DI ships `news_articles` with `available_at` + `symbols[]`, and (b) the Lead's
gate-read of THIS pre-reg. Research-only: NO quantlib / NO fingerprint; READ-ONLY stores. If the intensity
tier shows signal, a SEPARATE sentiment pre-reg follows — not this one.
