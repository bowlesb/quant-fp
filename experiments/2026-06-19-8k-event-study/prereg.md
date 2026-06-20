# 8-K EVENT-STUDY around the filing instant (PRE-REGISTRATION)

**Author:** Modeller · **Date:** 2026-06-19 (PT) · **Status:** PRE-REGISTERED — written BEFORE looking at
any outcome.

The FRAMING PIVOT out of the 4 settled cross-sectional DIRECTION-nulls (price ×2, order-flow, EDGAR+sector
#187). #187's ONE survivor — EDGAR filing activity → forward VOLUME (information-arrival intensity, net of
own-vol) — lives at the EVENT, not in a cross-sectional minute panel. So this study measures the abnormal
response AROUND each 8-K's filing instant directly: a per-EVENT window panel, not a within-timestamp
cross-section. It is also Ben's "when a ticker is HOT it matters, robust to minor feed-timing delay"
intuition, and it is the strategy-battery's Phase-1 (single-name / sequential-kernel) faithfulness target.

NO quantlib edits. NO fingerprint flip. Research deliverable. Stores READ-ONLY (`fp_store_real` bars +
Postgres `filings`/`sector_map`). Bounded `--rm` sandboxes.

---

## SUBSTRATE (verified before pre-registering — counts only, no outcomes)

- **8-K events:** Postgres `filings`, `form_type='8-K'`, look-ahead-safe `available_at` (SEC
  submissions_accepted). 2016→2026: ~21k→46k/yr, 2,000→4,100 distinct symbols/yr. **100% of 2018-2025 8-K
  symbols have minute bars** in `fp_store_real`.
- **Timing of the 8-K instant** (the entry rule driver): after-1600-ET **193,704** · pre-0930-ET **103,689**
  · RTH **71,274**. So MOST 8-Ks land outside RTH → the tradeable response is the NEXT regular session;
  the RTH subset gives a clean continuous intraday window.
- **Deep minute bars:** `fp_store_real` 2016→2026, 1-min OHLCV+vwap+trade_count, extended hours.

---

## EVENT / ENTRY / WINDOW DESIGN (tradeable, look-ahead-safe — pre-committed)

**Event time** `e` = `available_at + 5min` embargo (the #187 conservative lag — a filing is "actionable"
only 5 min after the SEC acceptance instant; never peek the filing-minute print).

**Two event regimes (analyzed separately — different entry, different overnight guard):**
- **RTH events** (`e` within 09:35–15:30 ET): the CLEAN intraday case. Entry = the first bar at or after
  `e`. Forward windows T ∈ {5, 15, 30, 60m} measured from the entry bar, all within the same RTH session.
- **OFF-HOURS events** (after-close or pre-open): entry = the NEXT regular session's tradeable open
  (first bar ≥ 09:35 ET of the next session). Forward windows {next-open→+30m, →EOD}. The overnight
  gap (prev-close → next-tradeable-open) is reported as the headline abnormal MOVE but flagged as an
  overnight-gap label (subject to the $1-floor + per-event winsor guards).

**Abnormality — TWO baselines (both pre-committed), the #187 own-vol-control lesson built in:**
1. **Own trailing baseline:** the event-window stat (volume, |return|, realized vol, range) divided by /
   minus the SAME stat over the name's OWN trailing baseline (a 20-session, same-time-of-day window
   ending the session BEFORE the event) → an own-normalized abnormality (controls for the name's size + its
   ambient vol level, which is what made 90% of #187's "EDGAR vol" collapse).
2. **Matched non-event control:** the same name's NON-event sessions at the same time-of-day (sampled,
   ≥10 controls/event) → the event-minus-control abnormality (a within-name difference-in-differences).
   An effect must show up against BOTH baselines to count.

---

## HYPOTHESES (pre-registered — 3, ranked by #187 prior)

### H1 — abnormal VOLUME / participation spike at the 8-K instant  *(prior: YES — the #187 survivor)*
**Claim:** the event window has abnormally high volume / trade_count vs both baselines — the
information-arrival participation surge. Stat: `vol_abnorm_T` = event-window volume / own-baseline volume,
and the matched-control difference. Tested at each T and both regimes.
**Predicted:** strongly positive, survives the event-timestamp shuffle + own-baseline normalization
(it IS the own-normalized stat) + OOS. This is the expected confirm — its job is to VALIDATE the framing
+ become the battery faithfulness anchor.

### H2 — abnormal MOVE-MAGNITUDE / realized vol  *(prior: the PRIZE if tradeable)*
**Claim:** the event window has abnormally large |return| / realized vol / high-low range vs both
baselines — a volatility expansion a straddle/range bet could harvest. Stat: `absret_abnorm_T`,
`rv_abnorm_T`, `range_abnorm_T`. **The tradeable test:** is the abnormal realized move LARGE ENOUGH, after
a realistic round-trip cost (5/10 bps each side + a representative ATM straddle premium proxy = the
own-baseline expected move), to be net-positive? I.e. does realized |move| > the cost of betting on it.
**Predicted:** positive abnormality likely (events ARE volatile); the OPEN question the #187 lesson forces
is whether it survives the own-vol control (is the event MORE volatile than that name's ambient vol, or
just a volatile name filing?) AND whether the net-of-straddle-cost edge is > 0. A "volatile but not
tradeable-net-of-cost" verdict is the honest likely outcome and is a clean result.

### H3 — DIRECTION drift post-8K  *(prior: NULL — the 5th direction test)*
**Claim:** signed forward return after the 8-K is non-zero (post-event drift / overreaction-reversal).
Stat: mean signed `ret_T` and its t-stat, plus a sign-split by the event-bar's own immediate reaction
(does the initial move continue or revert?). Tested but expected null (4 settled direction-nulls).
**Predicted:** null. If the average signed drift is indistinguishable from zero (and from the shuffle),
report the 5th clean direction-null honestly.

---

## DISCIPLINE (non-negotiable, pre-committed)

- **Tradeable entry** strictly AFTER `available_at + 5min`; off-hours events enter next-session-open — NEVER
  the filing-minute or 09:30 print.
- **Event-timestamp SHUFFLE baseline** (≥200 iters): re-draw each event's timestamp to a random
  same-name session/time, recompute the abnormality. The real abnormality must exceed the shuffled null
  (z). This is the event-study analogue of the cross-sectional within-block shuffle.
- **PREDICT-ZERO / unconditional baseline:** the non-event own-baseline distribution is the zero benchmark;
  the event must beat it.
- **OWN-VOL / SIZE CONTROL (#187 lesson):** the own-trailing-baseline normalization IS the control;
  additionally report the collapse ratio of the raw event stat vs the own-normalized stat — if the raw
  effect vanishes once own-baseline-normalized, it was size/vol, not the event.
- **OOS split:** fit the effect on 2018-2021 events, confirm on 2022-2025 (sign + magnitude consistency).
- **Net-of-cost:** any H2 tradeable claim is reported NET of round-trip cost (5/10 bps) + the straddle
  premium proxy; a signal that only survives at zero cost is non-tradeable.
- **$1-floor + per-event symmetric winsorization** on every overnight/multi-day window (the overnight-trap
  guard); label-std sanity check.
- **Multiple comparisons:** **Benjamini-Yekutieli FDR** (q=0.10) across ALL (hypothesis × window × regime)
  cells; the full cell count reported.

---

## STOP CONDITIONS (pre-committed)
- If H2 shows an abnormal move that survives the own-vol control + shuffle + OOS AND is net-positive after
  the straddle-cost proxy → **a genuinely net-new tradeable vol/magnitude edge** (the first non-direction
  edge). FLAG the Lead for a confirmatory disjoint-year replication BEFORE any excitement — no promotion
  this cycle.
- If H1 confirms (volume) but H2 is "volatile-not-tradeable-net-of-cost" and H3 nulls → the honest, likely
  outcome: the event surface is an INTENSITY/participation signal, not a tradeable directional or
  cost-positive vol edge. Report cleanly; it still anchors the battery's single-name archetype faithfulness.
- No post-hoc target/threshold tuning; any exploration beyond these pre-registered cells is labeled
  EXPLORATORY and excluded from the FDR family / any tradeable claim.
