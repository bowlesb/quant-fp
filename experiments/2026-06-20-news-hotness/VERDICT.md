# NEWS HOTNESS / INTENSITY — VERDICT (registered #230)

**Date:** 2026-06-20 · Pre-reg #230 (locked: H1 feature-utility reframe + provenance/embargo calibration).
Substrate: the DI backfill seed — 27,246 articles, 2025-11-12→2026-06-19 (~7 months), joined to the deep
`fp_store_real` bars. Panel: **109,187 obs, 957 names, 130 trading days**, tradeable entry ≥09:35 ET,
per-name point-in-time trailing-60d baseline, own-vol/size/baseline-coverage control, embargo {1,5,15}min
stability sweep. READ-ONLY. Code on this branch.

## TL;DR — NULL on BOTH legs. The orthogonal axis carries no net-new magnitude FEATURE value and no tradeable DIRECTION.

The one signal source genuinely orthogonal to all 9 prior price/microstructure nulls — pure news
HOTNESS/INTENSITY over a window, Ben's robust-to-feed-delay framing — is a clean null at our footing.
10th settled negative, and it's embargo-STABLE (not a feed-delay artifact). Two honest sub-results:

## H1 — HOTNESS → forward MAGNITUDE (feature-utility): NO net-new magnitude value

| feature | raw mag-IC (5m emb) | partial-IC after own-vol/size control | OOS | verdict |
|---|---|---|---|---|
| news_count_24h | −0.001 | +0.036 | FLIP | no value (raw ~0; partial SIGN-FLIPS) |
| news_hot_z_24h | −0.020 | +0.033 | FLIP | no value (raw small −; partial flips +; OOS flips) |
| news_burst_24h | −0.060 | +0.034 | consistent | no value (raw is the SIZE confound; sign-flips under control) |
| news_excl_24h | −0.032 | +0.018 | consistent | no value (raw small −; partial flips +) |
| news_velocity_24h | −0.001 | +0.036 | FLIP | no value |

**The raw hotness→|return| IC is ~0 (and where non-zero, NEGATIVE — a SIZE artifact, not a hotness signal):**
hotness correlates **+0.34 with size** (big names get more news), size correlates **−0.20 with forward
intraday |return|** (big names move less at 30m), so raw "hotness predicts |return|" is slightly negative
and entirely the size confound. Under the own-vol/size control the partial-IC FLIPS to a tiny +0.03 — i.e.
raw and partial DISAGREE IN SIGN, which is not "signal survives the control" but "there was no stable signal
to begin with." (forward |return| is driven by **own_vol, IC +0.48** = vol persistence — hotness adds
nothing net of it, the EDGAR #187 pattern.) Embargo-stable across {1,5,15}. **NO net-new magnitude feature.**

> Process note (the discipline): my first screen-gate naively flagged hot_z/burst as "NET-NEW FEATURE
> survives" because their COLLAPSE RATIO was >1 — but that was a near-zero-denominator artifact (collapse =
> |partial|/|raw| explodes when raw IC ≈ 0). I caught it, and corrected the gate to require a non-trivial
> RAW IC + SIGN-CONSISTENCY between raw and partial + OOS-consistency. Under the honest gate, none survive.
> A collapse>1 on a ~0 raw IC is meaningless, not survival.

## H2 — HOTNESS → forward DIRECTION (tradeable): CLEAN NULL

Direction ICs are tiny (+0.001 to +0.009), shuffle-z all < 2 (best `news_excl_24h` z=1.94 — does NOT
survive BY-FDR), and the net-of-cost decile L/S **MEDIAN is NEGATIVE for every feature × every embargo**
(−15 to −25 bps, win-rate 27–42%). No directional signal even pre-cost; cost buries what little there is.
The 10th settled direction-null, now on the news axis.

## Embargo-stability (the feed-delay gate) — PASSED as a robustness check
Every result is flat across EMBARGO ∈ {1,5,15}min (e.g. burst raw-IC −0.061/−0.060/−0.061). So the null is
NOT a feed-delay artifact — a count over a 1h/24h window is, as designed, insensitive to a few-minutes lag.
(This also means a real signal, had one existed, would have been delay-robust — the design was sound; there
just isn't a signal.) The p90 live-lag calibration from DI would only confirm the chosen embargo sits inside
the stable plateau; it cannot rescue a null that holds at EMBARGO=1.

## Disposition — NULL, no escalation (the pre-committed outcome)
- H1: no own-vol-independent, sign-stable magnitude IC → NOT a trustworthy net-new feature, NO promotion.
- H2: net-cost MEDIAN ≤ 0 at every embargo + no FDR-surviving direction IC → NULL, no replication flag.
10th settled negative — and the first on a genuinely orthogonal (information-arrival) axis. Honest result.

## What it settles + caveats
- The pure-INTENSITY tier of the news axis (counts/hotness/burst/exclusivity over windows) does NOT carry
  net-new magnitude-feature value or tradeable direction at our footing. Combined with the 9 priors, the
  meta-conclusion sharpens: across price, microstructure, AND information-arrival INTENSITY, we find no edge.
- CAVEATS (honest scope): (1) only ~7 months / 130 days of news — a SHORT panel; the per-name trailing-60d
  baseline leaves ~3 effective months of post-baseline screening, so the per-name z tier is the most
  data-starved. A longer backfill could revisit the z tier specifically. (2) SENTIMENT was deliberately
  EXCLUDED (a different parity/ML story) — this null is on INTENSITY only; a sentiment/relevance-content
  tier is a SEPARATE pre-reg, and is the one remaining news sub-axis not tested here. (3) The news is
  dilution-heavy (median 9 symbols/article, only 21% exclusive) — most "coverage" is market-wide; the
  exclusive-count tier (the cleanest single-name hotness) is also null but is the thinnest.

## Method / infra notes
- Compute-time join to `news_articles` (available_at + EMBARGO ≤ entry, symbols[] exploded — the filings/
  sector_map parity contract), per-name point-in-time trailing-60d count baseline (no look-ahead in μ/σ),
  forward 30m intraday |return| (magnitude) + signed return (direction) from the tradeable open.
- H1 gate = raw magnitude rank-IC + own-vol/size/base-coverage partial-IC + sign-consistency (NOT a
  net-cost-return gate — magnitude isn't tradeable on equity bars, per the #230 reframe). H2 gate =
  net-cost decile L/S MEDIAN. Shuffle (permute hotness→target within day), purge-OOS (month split,
  boundary purged), BY-FDR across (feature × embargo).
- Infra: chunked daily-cache + 3 embargo panels, ran clean in one detached named container.
