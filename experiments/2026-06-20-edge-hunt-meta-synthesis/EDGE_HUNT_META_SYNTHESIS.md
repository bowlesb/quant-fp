# Edge-Hunt Meta-Synthesis — where the search actually stands (strategic memo for Ben)

**Date:** 2026-06-20  **Author:** Modeller  **Purpose:** an honest, auditable synthesis of the whole edge
hunt so Ben can decide the next strategic move. Not a hunt; a map. Every claim below traces to a committed
verdict (PR # cited).

---

## TL;DR (the three things that matter)
1. **No single new signal we tested adds incremental tradeable $ over the baseline.** Most axes are settled
   nulls. But the nulls split into two very different kinds — "no signal" vs "real signal that doesn't clear
   tradeable cost/noise/survivorship" — and that distinction is the whole story.
2. **The edge we actually have is the PORTFOLIO/MODEL, not any one signal.** The combined trusted-baseline
   model is **net-positive under accurate (Stage-1 measured) cost** — +$123,579 headline-10%, Sharpe 18.9
   (PR #271). Individual signals are individually-weak; the assembled model is not.
3. **The binding constraints, ranked: (1) survivorship (a DATA problem), (2) net-of-cost (now measured, the
   cost model helps), (3) single-signal basket noise (a PORTFOLIO problem).** Each points at a different
   next-move, and only one of them is "invent another signal" (it isn't the top one).

---

## 1. The comprehensive NULL-MAP (every axis, and WHY each failed)

The critical column is **failure mode** — it separates "nothing there" from "something real that we can't
trade yet," because those route to completely different next-moves.

| Axis | Verdict | Gross signal? | Failure mode | PR |
|---|---|---|---|---|
| Price-DIRECTION (intraday/overnight, ~5 framings) | NULL | No (NW\|t\|<1.2, IC < shuffle) | **No signal** — direction is unpredictable cross-sectionally at our scale | (pre-session) |
| Order-flow cross-sectional (0/4) | NULL | No (NW\|t\|<1.2) | **No signal** — trade_freq/book_depth dominate gain = intensity, not alpha | #orderflow |
| Lane D EDGAR / sector / news | NULL | No for return (predicts VOLUME) | **No signal** for return — an information-arrival→participation effect, not directional alpha | #187/#197 |
| Price-GEOMETRY — swing_dc as $ | NULL | DROPS (AUC .535→.533, rankIC +.064→+.061) | **Redundant** — the baseline already prices the path/vol structure | #259 |
| Price-GEOMETRY — path-geometry G0 | NULL | DROPS (AUC .529→.526) | **Redundant** — same; ~91% of tail edge already in baseline shape+vol groups (#255) | #263 |
| Quote-ALPHA (quote-dynamics G0a) | NULL | **YES, IMPROVES** (AUC .529→.536, rankIC +.046→+.058) | **Cost-killed** gross-up/net-down — real ranking gain, dies net at every conservative cut | #268 |
| Quote-alpha RE-GRADE under accurate cost (Option A) | NULL | (re-test of above) | **Cost-confirmed** — accurate cost makes the broad liquid head net-WORSE (78% of names dearer than the old stub); not a cost-measurement artifact | #275 |
| Weekly-REVERSAL (multi-day horizon) | NULL on bar | **YES, real + clean** (rank-IC +0.025 both windows, 11σ vs shuffle, own-vol-INDEPENDENT) | **Basket-noise + survivorship** — IC significant but per-week dollar basket NW-t<2; the −13bps survivorship haircut flips replication +11.5→−1.2bps | #287 |

**The map reads in three bands:**
- **Genuinely no signal (4 axes):** price-direction, order-flow, EDGAR/sector/news (for return), — these are
  *settled*. Cross-sectional direction prediction and event-arrival are not alpha at our scale. Stop here.
- **Real signal, but REDUNDANT with the baseline (2 axes):** swing_dc + path-geometry magnitudes. The signal
  is real but the baseline already captures it — adding it drops incremental rank. (Features still retained.)
- **Real signal, NOT redundant, but UNTRADEABLE (2 axes):** quote-dynamics (gross-up/net-down = pure cost
  kill) and weekly-reversal (real 11σ IC, killed by basket noise + survivorship). **These are the
  interesting ones** — there IS exploitable structure; the blocker is cost/noise/data, not absence of signal.

---

## 2. The real-but-untradeable signals we FOUND and RETAINED (the asset inventory)

Per the inclusion-liberal principle (a $-null is a model question, not a feature-inclusion gate), these are
all **kept in the store/bus** and are genuine, validated structure — just individually weak or cost-blocked:

- **Weekly-reversal `rev_1w`** — rank-IC +0.025, 11σ over shuffle, **fully own-vol/size-independent**,
  sign-consistent across 2018-21 and 2022-25. The single cleanest signal of the hunt. Untradeable today only
  because the per-week dollar basket is noisy (NW-t<2) and the survivorship haircut eats the recent dollars.
- **swing_dc magnitude (`dc_resp_chunk_slope` et al.)** — the first replicated non-null (partial-IC
  +0.143→+0.148 on a disjoint window, own-vol-independent). A real path-ROUGHNESS magnitude predictor; just
  redundant with the baseline for $ and directionally null.
- **Path-geometry / volatility-structure magnitudes** — the #255 tail-importance read: ~91% of the model's
  profitable-tail edge concentrates in price-return-shape + vol structure. Real, and *already in the
  baseline*.
- **Quote-dynamics (spread vol/trend, imbalance, staleness, LP-replenishment)** — improved gross ranking
  (AUC/rankIC up); a real microstructure signal that dies purely on net cost.
- **The Stage-1 realized cost model** — not alpha, but validated INFRA: predicts realized half-spread OOS
  R²=0.575, IC=0.902, 59% MAE-cut vs the flat stub. It is the measurement that made every verdict above
  honest.

We are NOT signal-poor. We are signal-rich and TRADEABILITY-poor.

---

## 3. THE KEY ASYMMETRY (the most important strategic fact)

**No single NEW signal adds incremental tradeable $ over the baseline — yet the COMBINED trusted-baseline
model is net-positive under accurate cost.**

- Stage-1 before/after (PR #271), the trusted-baseline harness L/S, 42 dates / 3,621 OOS rows, booked under
  REALIZED per-name cost (not the flat stub): **headline-10% = +$123,579, Sharpe 18.9** (a −22% haircut from
  the flat-stub +$158k, but still solidly positive, and now *honest*). The tail cuts are stronger still.
- Every incremental-signal test (swing_dc, path-geometry, quote-dynamics) was an A/B *on top of this
  baseline* and added nothing — because the baseline is already a well-diversified combination that prices
  most of the structure a single new feature carries.

**Implication:** the edge we possess is the **portfolio/model**, not any one signal. The hunt has been
implicitly asking "what new signal beats the model?" — the more productive question is "is the model we
ALREADY have good enough to deploy, and how do we harden it?" That reframing is the main output of this memo.

---

## 4. The BINDING CONSTRAINTS, ranked (each routes to a different move)

1. **SURVIVORSHIP (a DATA problem) — the hardest, and the one blocking our best real signal.** The deep panel
   is perfectly survivors-only (0/600 names delist in-sample; verified). Weekly-reversal's recent-window
   dollars depend entirely on whether the delisted losers are present — they aren't. We can only *charge* an
   external haircut, not *measure* the truth. This caps every loser-buying / mean-reversion / multi-day
   strategy. Only a delisting-inclusive universe removes it.
2. **NET-OF-COST (now MEASURED; the cost model helps).** Was the silent killer of every gross signal; Stage-1
   made it honest and asymmetric (the flat stub was 2.8x too high for the liquid head, too low for the tail).
   Quote-alpha and most intraday signals die here. This is now a *measured, managed* constraint — the cost
   model can also become a forward (Stage-2) tool to TIME entries to cheap moments. Less of a wall than it was.
3. **SINGLE-SIGNAL BASKET NOISE (a PORTFOLIO problem).** Weekly-reversal's IC is significant (t≈2.4) but its
   weekly dollar basket NW-t<2 — one signal's basket is too noisy to be significant week-to-week. This is the
   classic case for COMBINING weak-but-real signals into a portfolio whose aggregate is significant even when
   each leg isn't. We have several real-but-weak signals (§2) and have never combined them.

---

## 5. RANKED NEXT-MOVES (honest cost/benefit) + my recommendation

### (b) Multi-signal PORTFOLIO combination of the real-but-weak signals — **MY #1 RECOMMENDATION**
- **What:** stop hunting new single signals; combine the real-but-weak ones we ALREADY have and retain
  (weekly-reversal, swing_dc magnitude, path/vol structure, quote-dynamics-as-features) into one model and
  test the AGGREGATE net-of-(measured)-cost — does the portfolio clear when no leg does?
- **Why #1:** it directly attacks constraint #3 (basket noise), uses ONLY data/signals we already have (zero
  acquisition cost, days not weeks), uses the now-accurate cost, and tests the §3 asymmetry head-on ("the
  edge is the portfolio"). It's also the cheapest decisive experiment left.
- **Cost/risk:** low cost; risk is that the signals are correlated (all priced by the baseline) so the
  portfolio adds nothing — but that itself is a decisive, cheap answer.
- **Caveat:** weekly-reversal's leg still carries the survivorship caveat; report the portfolio with and
  without it.

### (c) Validate / harden the existing net-positive model toward DEPLOYMENT — **MY #2 (do in parallel)**
- **What:** the baseline model is net-positive under realized cost. Treat THAT as the asset: stress it (more
  dates, regime splits, capacity/turnover under realized cost, the Stage-2 cost gate), confirm it holds, and
  move it toward paper→real deployment. Ben's whole objective is a deployable system, and we may already have
  a (modest) one and keep walking past it hunting for a better single signal.
- **Why high:** turns research into the thing Ben actually wants (deployment), and it's low-novelty/high-
  certainty. The risk is the edge is thin/capacity-limited — but we won't know without hardening it.

### (a) Acquire a delisting-inclusive (CRSP-style) universe — **#3 (high-value but slow/external)**
- **What:** the one move that removes constraint #1 and could make weekly-reversal's real +0.025 IC tradeable
  by turning the haircut from an external estimate into an in-sample measurement.
- **Why not #1:** it's a data-acquisition project (cost, time, licensing) with an uncertain payoff (the IC
  might still not clear once measured honestly). High expected value but high latency — worth *starting* in
  parallel, not blocking on.

### (d) Deeper quote microstructure — **#4 (lowest, the vein is largely worked)**
- Quote-ALPHA already nulled (G0a + the accurate-cost re-grade). More quote microstructure is more of a
  worked vein. The quote tape's real residual value is the COST model (Stage-2), not new alpha. Deprioritize
  as an alpha source.

### My honest single recommendation
**Pivot from single-signal hunting to (b) portfolio-combination of the real-but-weak signals + (c) hardening
the net-positive baseline toward deployment, in parallel; start (a) delisting-inclusive data acquisition as a
slow background track for the one genuinely-promising blocked signal (weekly-reversal); deprioritize (d).**
The §3 asymmetry is the tell: we have a portfolio edge and a pile of real-but-weak signals, and we've been
testing them one-at-a-time against a model that already beats them individually. The highest-EV, lowest-cost
move is to combine what we have and to take the model we already have seriously as a deployable asset —
rather than keep paying for new single signals that the portfolio already prices.

---

## Appendix — what "null" means here (the disposition principle, for the record)
Every "null" above is a *what-the-model-trades* verdict, not a feature-worthlessness verdict. All features
tested are retained in the store/bus (inclusion is liberal and decoupled from $-value; cheap to carry;
future data + interactions may use them). This memo maps tradeability, not feature value.
