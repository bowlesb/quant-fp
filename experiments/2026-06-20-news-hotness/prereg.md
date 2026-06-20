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
`available_at + EMBARGO <= ctx.timestamp` — exactly the mechanism of the EDGAR filing-frequency features.
This is RT-trivial (a windowed count from the store) and parity-true by construction MODULO the feed-delay
provenance gap below (deterministic from the `news_articles` store + `ctx.timestamp`; the same compute-time-
join contract as `sector_map`/`corporate_actions`/`filings`). NO point-in-time headline NLP, NO embeddings,
NO FinBERT on the fast path (the old repo's embeddings/finbert tiers are explicitly OUT — heavy + a
different parity story). Counts + a SIMPLE keyword/relevance flag only.

⚠️ **`available_at` PROVENANCE GAP (DI-flagged — the parity claim is parity-true MODULO this, not absolutely):**
DI reports the store's `available_at` differs BY SOURCE (`available_at_source`): LIVE = the websocket arrival
instant; BACKFILL = Alpaca `created_at`. So the SAME article has a DIFFERENT `available_at` live vs
backfilled, by the feed-delay between `created_at` and ws-arrival. Therefore the live/backfill values are NOT
byte-identical — parity holds only MODULO that lag. The EMBARGO + the EMBARGO-stability test (§feed-delay)
are the explicit defense: a hotness count over a 1h/24h window is barely perturbed by a few-minutes
provenance gap, and the stability test FALSIFIES any signal that isn't. The research hunt runs on the
BACKFILL `created_at + EMBARGO` (the look-ahead-safe historical instant); the EMBARGO is CALIBRATED to the
real lag (§feed-delay), not assumed.

## DATA CONTRACT (what the design assumes DI's store provides — pre-committed)
`news_articles(id, available_at, symbols[], headline, source, ...)` with:
- `available_at` = the look-ahead-safe first-sight dissemination instant (the SAME discipline as `filings`),
  microsecond. THIS is the only timestamp features may read (NOT a `published_at` that could be revised).
- `symbols[]` = the tickers tagged to the article (for per-name attribution + the exclusivity/dilution
  relevance refinement).
- `headline` (for a simple regex headline-mention + clickbait/market-wide flag — keyword tables ported from
  the old repo's relevance.py, NO ML).
- `available_at_source` (the DI-flagged provenance field: `atom_feed`/ws-arrival vs backfill-`created_at`) —
  required so the research can run on BACKFILL `created_at` and so DI can measure the live-vs-backfill lag
  for the EMBARGO calibration (§feed-delay).
If the store ships without `symbols[]`, a reliable `available_at`, or `available_at_source`, this design does
NOT run as specified — flagged to DI.

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
- `EMBARGO` (default `= 5 min`, but CALIBRATED to the measured lag — see below, NOT a pure assumption): a
  feature at `T` only counts articles with `available_at + EMBARGO <= T`. Since the research runs on BACKFILL
  `created_at`, the embargo is the buffer between `created_at` (when the article was authored/disseminated)
  and when we could ACTUALLY have acted on it LIVE (ws-arrival). It ensures we never count an article we
  couldn't have traded on.
- ⭐ **EMBARGO CALIBRATION (the provenance fix — tie the embargo to DATA, not a guess):** ask DI to measure
  the distribution of `(live ws-arrival `available_at`) − (backfill `created_at`)` over articles present in
  BOTH the live and backfill stores — the REAL feed-delay. The verdict EMBARGO is set to a HIGH percentile
  (frozen: the **p90** of that measured lag) so the historical signal only uses articles that would have been
  live-actionable. **A signal that requires an EMBARGO SMALLER than the measured real lag is NOT tradeable**
  — the calibration enforces exactly that. Until DI ships the measurement, the run uses 5 min as a placeholder
  AND the stability sweep below brackets it.
- **EMBARGO-STABILITY TEST (the falsifiable feed-delay gate, load-bearing):** re-run every result under
  EMBARGO ∈ {1, 5, 15} min (bracketing the likely measured lag). A real hotness signal is STABLE across the
  sweep (a 1h/24h count barely moves if articles are 5–15 min late); a signal that exists only at EMBARGO=1
  and dies by EMBARGO=15 is a delay artifact → reported fragile/non-actionable, regardless of H1 or H2. This
  test, tied to the measured lag, is the operationalization of Ben's "robust to feed delay."

## HYPOTHESES (pre-registered — 2)

### H1 — HOTNESS → forward MAGNITUDE: a FEATURE-UTILITY evaluation (NOT a tradeable-return claim)
**Claim:** an abnormal coverage spike (`news_hot_z`, `news_burst_ratio`) predicts elevated forward realized
move-magnitude / vol / volume — the information-arrival intensity effect (the axis that survived weakly in
EDGAR #187). Targets: forward |return|, forward realized vol, forward volume over {30m, EOD, next-day}.

⚠️ **H1 IS EXPLICITLY A FEATURE-UTILITY / MAGNITUDE-PREDICTION RESULT, NOT A STANDALONE TRADEABLE EDGE — and
the success gate is NOT a net-cost-return median.** The "intensity ≠ alpha" trap (EDGAR #187 settled exactly
this): forward |return| / realized vol cannot be traded directly on equity minute bars — we hold NO options
/ vol instruments, so "hotness predicts vol" can be TRUE (pass an IC test) yet be UN-MONETIZABLE. To avoid
mislabeling a vol-prediction as an edge:
- **H1 success = the hotness feature adds magnitude-prediction power that SURVIVES the own-vol/size/baseline-
  coverage control** (the battery's non-direction / magnitude-feature family). Gate = **rank-IC of hotness
  vs the forward-magnitude target, with the partial-IC COLLAPSE RATIO after the own-vol control as the
  decisive number** (net-new magnitude info beyond "this name is already volatile"), + shuffle + embargo-
  stability + purge-OOS. The **net-cost-return MEDIAN gate is DROPPED for H1** — it does not apply to a
  magnitude target.
- **PROMOTION for H1 means: "a trustworthy NET-NEW magnitude FEATURE for the model"** (a feature the
  strategy-battery / a downstream model can consume for vol-aware sizing), explicitly NOT "a tradeable
  strategy." All "tradeable edge" language is reserved for H2.
- Honest prior: like EDGAR #187, the raw hotness→vol IC will likely be high but mostly COLLAPSE under the
  own-vol control (hot names are already volatile names). H1 "succeeds" only if a residual, own-vol-
  independent magnitude signal survives — reported as a feature-trust result, never as alpha.

### H2 — HOTNESS → forward DIRECTION: the TRADEABLE test (skeptical prior; the net-cost-median gate bites here)
**Claim:** abnormal coverage predicts signed forward return (over/under-reaction drift) — a DIRECTIONAL bet
you can actually trade (take the predicted sign, pay the spread, measure net return). Prior: SKEPTICAL —
direction has nulled 9×. Tested as a raw signed-return predictor AND conditioned on the relevance tier
(exclusive/headline coverage may carry more directional info than diluted market-wide mentions). **This is
where the NET-OF-REAL-COST MEDIAN gate applies** (you trade the sign → the median net return is well-defined
and is the verdict); in the FDR family; a favorable mean can't reopen it.

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
- **NET-OF-REAL-COST MEDIAN gate — H2 (DIRECTION) ONLY:** the H2 verdict stat = net forward P&L MEDIAN after
  realistic cost (the real effective spread from the now-queryable quote tape on the overlap window, else
  5/10bps), MEDIAN-anchored (a favorable mean CANNOT reopen it). It does NOT apply to H1 (magnitude is not a
  tradeable return — H1's gate is the own-vol-control partial-IC collapse, per §H1).
- **$1-floor + per-period winsor**; **EMBARGO-stability** test (§feed-delay) for BOTH hypotheses.
- **BY-FDR** across all (hypothesis × window × target × embargo) cells.

## KILL CONDITIONS (stated in advance — H1 feature-utility, H2 tradeable; both falsifiable)

### H1 (magnitude FEATURE-utility — gate = own-vol-control collapse, NOT net-cost-return)
- **NULL / no feature value** iff the hotness→forward-magnitude IC COLLAPSES under the own-vol/size/baseline-
  coverage control (it was "big liquid/already-volatile names," not news-driven), OR it is not EMBARGO-stable
  (a delay artifact), OR it fails the shuffle. This is the LIKELY outcome (the EDGAR #187 result).
- **PROMOTABLE AS A FEATURE (not a strategy)** iff a residual own-vol-INDEPENDENT magnitude IC survives the
  control (collapse ratio stays high) AND is EMBARGO-stable AND purge-OOS-consistent AND beats shuffle →
  report as a trustworthy NET-NEW magnitude feature for the model/battery. Explicitly NOT "a tradeable edge";
  no net-cost-return claim is made or implied for H1.

### H2 (DIRECTION — the tradeable test, median-anchored)
- **SETTLES NULL** iff the net forward-return MEDIAN ≤ 0 on the verdict setting REGARDLESS of mean, OR it is
  not EMBARGO-stable, OR it fails shuffle. (Prior: this is the likely outcome — direction has nulled 9×.)
- **REOPENS / promotable as a TRADEABLE EDGE** iff net-of-cost MEDIAN > 0 AND survives shuffle + own-vol/size
  control AND EMBARGO-stable AND purge-OOS-consistent → the FIRST orthogonal-axis tradeable edge → FLAG the
  Lead for confirmatory replication BEFORE excitement.
- **NON-ROBUST** (reported, not promoted) iff a positive median appears only at one embargo / window / OOS
  half / before the own-vol control.

## RUN PLAN (data-gated; design is this cycle's deliverable)
Build (`build_news_panel.py`) joins the `news_articles` store to the deep bar/quote panel at compute time
(the same parity-true compute-time join as `filings`/`sector_map`), emits the per-(name, entry) hotness
panel; screen (`screen.py`) = the median-anchored net-of-cost gate + shuffle + purge-OOS + own-vol control +
embargo-stability + FDR. Reuses the #205/#212/#226 host-mounted resumable cache + chunked-subprocess infra.
Build starts only after (a) DI ships `news_articles` with `available_at` + `symbols[]`, and (b) the Lead's
gate-read of THIS pre-reg. Research-only: NO quantlib / NO fingerprint; READ-ONLY stores. If the intensity
tier shows signal, a SEPARATE sentiment pre-reg follows — not this one.
