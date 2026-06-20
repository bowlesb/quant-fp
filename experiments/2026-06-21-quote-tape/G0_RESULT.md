# Quote-tape G0 $-screen — VERDICT: G0a (alpha) NO-GO, G0b (cost model) KEEPER

**Date:** 2026-06-20  **Gate:** G0a (alpha $-screen) + G0b (cost-model deliverable), both on THROWAWAY
proxies BEFORE any production group/kernel (G0-first standard). Substrate: 42 well-covered dates
2026-04-15..06-12, top-200 liquid, entry 09:40 ET, forward-30m, quote tape sliced to [T-60m, T+30m].
Panel 4,607 rows / 49 entry-days, 148 baseline feats (trusted + existing `quote_spread` 21-feat group),
9 quote-dynamics proxies + 1 realized-cost label, ALL 100% populated. G-STALE enforced (entry reads
quotes ts<T strict µs; label reads ts>=T; valid-NBBO filter). Walk-forward GBM, 5 folds → 25 OOS days,
3,621 OOS rows. EXACT Thread-1 harness path (shared decide-core, shuffle + predict-zero).

## G0a — ALPHA (quote-dynamics predict forward magnitude net of cost): **NO-GO**
Quote-dynamics proxies (spread vol/trend, imbalance mean/trend, depth log/vol, quote intensity, staleness)
added to the FULL baseline + `quote_spread`. They IMPROVE gross ranking but DEGRADE net-$ at every
conservative cut — the binding-constraint failure (real gross, dies net), the same lesson as every prior
null, here in its cleanest form (AUC even goes UP while net-$ goes DOWN).

| cut | A baseline $ | B +quote-dyn $ | Δ total $ | Δ prec | Δ Sharpe |
|----:|----------:|----------:|----------:|-------:|---------:|
| 2% | +303,714 | +157,650 | **−146,064** | +0.0439 | +1.40 |
| 5% | +180,817 | +102,092 | **−78,725** | +0.0058 | −0.77 |
| 10% | +134,468 | +121,573 | **−12,895** | +0.0014 | −4.74 |

- AUC 0.5293→0.5360 (UP), rank-IC +0.0462→+0.0576 (UP) — gross ranking IMPROVES, but net-$ falls at all
  conservative cuts. The proxies re-rank into names that cost more / pay less net.
- Robustness: per-day 5%-cut L/S excess is statistically IDENTICAL gross (BASE +114.2bps t=2.11 vs +QD
  +113.7bps t=1.92) — adding quote-dynamics does NOT lift the per-day basket; it slightly lowers t. The
  net-$ drop is not hiding a real gross gain.
- Shuffle baselines negative both arms (no leakage). predict-zero $0.

**G0a verdict: NO-GO** — quote-dynamics add no tradeable net edge over a baseline that already holds the
static `quote_spread` snapshot. Do NOT build a quote-dynamics ALPHA feature group on this evidence.

## G0b — COST MODEL (the deliverable, per Lead): **KEEPER — wire into the harness**
A walk-forward GBM predicting each name's realized forward time-weighted half-spread from the SAME trailing
quote proxies is HIGHLY accurate out-of-sample:

- **OOS R² = +0.575, rank-IC = +0.902** (3 valid walk-forward folds; the first 2 of 5 had <600 train rows on
  the thin early-April dates).
- **MAE: model 2.08 bps vs flat-stub 5.12 bps = 59% error reduction.**
- **Realized fwd half-spread: mean 7.89 bps, median 6.47, p10–p90 = 1.90–15.55 bps** — vs the harness's flat
  `DEFAULT_HALF_SPREAD_BPS = 3.0`. The flat stub UNDERCHARGES by ~2.6x on average and captures ZERO of the
  4–8x per-name spread variation the model recovers.

**Why this is a KEEPER even though G0a nulled (the Lead's exact point).** Every net-of-cost verdict this
platform has made — including the 3 path-structure nulls — charged the flat 3.0bps stub. The ARM C check
shows the distortion directly: the SAME baseline signal books headline-10% $ = +$144,823 under the flat stub
but +$118,165 under realized cost — an **18% optimism haircut**. Our prior nulls were graded UNDER-costed, so
a sharper cost term makes them MORE null, not less — it strengthens every past verdict and every future one.
This cost model is INFRASTRUCTURE: wire the per-name predicted half-spread into `long_short_per_name_cost`
to replace the flat stub, and every harness $-test + the live executor become cost-ACCURATE.

## Net read
- The quote tape did NOT yield a cross-sectional ALPHA edge (G0a null) — consistent with the standing
  meta-conclusion that net-of-cost cross-sectional alpha is hard at our scale.
- It DID yield the highest-leverage thing available: a validated per-name COST model that retroactively
  sharpens every gate. Cost-accuracy was the framing the Lead prioritized, and it paid off.
- G1-G6 + G-STALE rigor NOT run for G0a (it nulled). For the COST MODEL, the next step is productionization
  (a per-name-cost feature/service + the harness wiring), pending the Lead's call — it is a cost-INPUT change
  (data), not a decide-core change, so portability is trivial.

## G5 data-sizing answer (for the quote-depth backfill the Lead is prioritizing)
Verified: the broad ~4,031-sym quote breadth begins EXACTLY at **2026-03-31**; before that, back through
2025-09, only a **531-sym head-only** set exists. So the current broad window is 2026-03-31..06-12 (~50
trading days). For a genuinely-disjoint-quarter G5 OOS replication (not a within-quarter split), the backfill
must **extend the ~4,000-sym BREADTH backward from 2026-03-31 to at least 2026-01-01** (one prior quarter),
ideally 2025-10-01 (two quarters). It is a breadth-at-depth fill (the ~3,500 non-head names before 03-31),
NOT new head dates (the 531 head names already reach 2024-12). Non-blocking for G0/the cost model (done on
the current window); needed only to make a quote-ALPHA G5 a separate-regime test — but since G0a nulled, the
backfill's near-term value is for the COST MODEL's robustness across a wider regime, not a quote-alpha retry.
