# PRE-REGISTRATION — Quote-tape microstructure hunt (alpha AND execution/cost-timing)

**Date:** 2026-06-20  **Author:** Modeller  **Status:** GATE-READ REQUESTED (no production code built yet)
**Lane:** deep QUOTE / TAPE microstructure — the genuinely orthogonal axis after THREE path-structure-
magnitude $-nulls on the bar-derived baseline (swing_dc-as-$, #255's "~91% of tail edge already in
baseline", the path-geometry G0). Different DATA SUBSTRATE (raw NBBO quote tape, not minute-bar geometry),
so not redundant-by-construction with the baseline that absorbed the geometry signal.

This commits the thesis, construction, the DATA-CONSTRAINT it needs, and the full pre-committed gate BEFORE
any code — and it leads with the **G0 cheap $-screen** (now standard protocol). A null is a publishable answer.

---

## 0. Protocol carried forward (the standing methodology, earned over 12 hunts)
- **G0-FIRST IS STANDARD.** Lead with a cheap THROWAWAY-PROXY $-screen on the binding constraint (incremental
  net-$ over the FULL baseline) BEFORE any production feature/kernel. The bar-geometry vein cost us a near-
  kernel build before G0 caught the null; we never pay that again. G1-G6 rigor still runs, but only after G0
  says there is net-$ to chase.
- **The binding constraint is NET-OF-COST**, every time. Every null we hit was real gross and died after
  spread/impact. That is WHY the quote tape is the right pivot: it is the one substrate that attacks COST
  directly (framing (b) below), not just another gross-alpha candidate that dies net.
- Median-anchored, $1 floor, tradeable entry, own-vol control as THE IC gate, BY-FDR, disjoint-window OOS,
  no-look-ahead bit-identical — unchanged.

---

## 1. The thesis — TWO framings, both screened at G0 (pre-register both)

### (a) ALPHA framing — quote structure predicts forward MAGNITUDE/return net of cost
**Claim.** Quote-tape DYNAMICS — not the static spread/imbalance snapshot the existing `quote_spread` group
already holds, but the *time-evolution* of the book — carry net-new cross-sectional information about the
forward 30m move, surviving cost. Concretely: spread-volatility / spread-trend over the trailing window,
quote-update intensity (quotes/sec) and its burstiness, quote staleness (time since last NBBO change),
top-of-book depth imbalance EVOLUTION (not the level), and LP-replenishment speed (how fast depth refills
after it is hit). The #205 spread re-test + liquidity-provision surface lives here.

### (b) EXECUTION / COST-TIMING framing — quote structure predicts realized COST (the higher-leverage angle)
**Claim (the one worth more to us right now).** Quote features predict the *realized half-spread / impact*
a name will cost to trade over the next window, well enough to (i) replace the flat `DEFAULT_HALF_SPREAD_BPS`
cost stub with a per-name PREDICTED cost, and/or (ii) TIME entries to tight-spread / deep-liquidity moments
so the SAME gross signal clears cost it otherwise dies under. A feature that shaves realized cost converts
already-discovered gross alpha into net $ — directly attacking the binding constraint, not adding a new
fragile gross signal. This is the more defensible bet given our null history.

**Why net-new / orthogonal (the pivot cuts both ways here — G2 confirms, but the prior is favorable).**
- Different substrate: bar-derived shape/vol groups CANNOT see intra-minute book dynamics, quote staleness,
  or replenishment — these are sub-bar quote-tape observables. The baseline absorbed all the geometry; it
  has zero quote-dynamics content.
- BUT a `quote_spread` group already exists (21 feats: `spread_bps_*`, `quote_imbalance_*`, `book_depth_*`)
  — so G2 must prove incremental over the baseline AND over that existing static-quote group: the net-new
  axis is DYNAMICS + STALENESS + COST-PREDICTION, not another spread snapshot.

**Direction discipline.** Framing (a) is MAGNITUDE/risk (predict |move|/range), not signed direction (11
direction nulls). Framing (b) predicts a COST observable (realized spread/impact) — a regression target we
can measure directly from the tape, the cleanest possible label.

---

## 2. Construction (point-in-time, quote-staleness-safe, parity-portable)

Per name at the entry instant T (tradeable >=09:35 ET), from the raw NBBO quote tape `[T-W .. T)` (W=30–60m;
fields available and verified: `ts` µs-UTC, `bid_price/bid_size/ask_price/ask_size`, exchanges, conditions):
- **Spread dynamics:** mean / std / trend of the relative spread `(ask-bid)/mid` over the window; the time-
  weighted (not count-weighted) spread (a quote that stood for 5s counts 5x a quote that stood 1ms).
- **Depth & imbalance dynamics:** top-of-book size imbalance `(bid_size-ask_size)/(bid_size+ask_size)`, its
  mean AND its trailing change; depth level (log total size) and its volatility.
- **Quote intensity & staleness:** NBBO updates/sec and burstiness; **quote age at T** = T − (last NBBO
  change ts) — the staleness observable (a stale quote is an unreliable entry price; this is both a feature
  AND a no-look-ahead guard, §G-STALE).
- **LP replenishment (the #205 surface):** after a depth depletion event, how fast does top-of-book size
  refill — a liquidity-provision-strength proxy.
- **(framing b) realized-cost label:** the time-weighted effective half-spread actually quoted over `[T, T+h]`
  (and, where the trade tape overlaps, the realized signed effective spread of executions) — the COST the
  harness should charge, measured rather than stubbed.

**Quote-staleness / NBBO-timing discipline (NAMED GATE — quotes make no-look-ahead HARDER):**
- The entry reads ONLY quotes with `ts` strictly `< T` (the decision instant). The forward-cost label reads
  `ts >= T`. No quote whose `ts == T` or later may inform the entry feature (the off-by-one that leaks the
  future at µs resolution).
- A name whose newest quote at T is older than a staleness cap (e.g. > 5s) is FLAGGED (feature emitted, but
  the entry is marked unreliable) — never silently used as if fresh.
- Build features off the time-weighted quote state, asof-joined with `strategy="backward"` (last quote at or
  before each grid point), NEVER forward-fill across T.

---

## 3. Production-portability note (decided BEFORE building)
- **Most quote features are COLUMNAR** over the per-name quote frame — rolling spread/imbalance/depth
  aggregations + an asof-join to the entry grid are pure polars. → implement in the feature group, parity-
  by-construction (live bus quote stream == backfill quote tape: same polars expression on the same frame).
- **Quote-intensity / staleness / replenishment-event statistics MAY be SEQUENTIAL** (per-quote state: time
  since last change, refill-after-depletion). If so, same swing_dc pattern: a shared Rust kernel in
  `quant_tick` pinned cell-for-cell by a pure-Python reference oracle (no-look-ahead + fold==reseed tests),
  called identically from the live quote stream and the backfill tape. PREFER columnar; escalate to a kernel
  only for the genuinely per-quote-stateful statistics.
- **The cost-timing output (framing b)** plugs into the harness cost model directly: a per-name predicted
  half-spread REPLACES `DEFAULT_HALF_SPREAD_BPS` in `long_short_per_name_cost` — a change to the cost INPUT,
  not the decide-core; portability is trivial (it is data, not logic).

---

## 4. DATA-CONSTRAINT REALITY (design within it — flag to Lead)
**Verified now (read-only survey of /store/raw/quotes):**
- **Breadth: ~4,031 symbols quoted per date in the eval window** (2026-04-15..06-12) — confirmed on
  04-15/05-15/06-10/06-12. This MATCHES the trusted-feature eval window → a CROSS-SECTIONAL quote hunt is
  FEASIBLE NOW on the same 42 well-covered dates the path-geometry G0 used.
- **Depth: head names go back to 2024-12** (symbol A has 2024-12-18 onward), but breadth-at-depth is the open
  question — the ~3mo "fully-broad" window (2026-03..06) is what the Lead flagged; OLDER dates are head-only.
- The 11 SPDR sector ETFs zero-quote fill (#260) is now on main — sector-relative quote features become
  possible once those are populated.

**What THIS hunt needs (flag for prioritization — Ben's standing #1 raw-data priority):**
- For the G0 cross-sectional screen: NOTHING new — the existing ~4k-sym × 42-date window suffices. Proceed now.
- For a robust disjoint-window OOS (G5) and any depth-trend feature with a long lookback: **broader quote
  depth going back past 2026-03** would let the held-out window be a genuinely separate quarter rather than a
  split of one quarter. Flagging the **quote-depth backfill** (deepen breadth before 2026-03) as the enabling
  data task — but it does NOT block G0/G1-G4 on the current window.

---

## 5. The gate — PRE-COMMITTED pass/fail (G0 first; G1-G6 only if G0 is GO)

Substrate: ~4k-sym × 42-date eval window, forward-30m cross-sectional EXCESS label (framing a) / realized
time-weighted half-spread label (framing b), $1 floor, tradeable entry >=09:35 ET, two disjoint windows.

| # | Gate | Pass bar | Why |
|---|------|----------|-----|
| **G0a** | **CHEAP $-SCREEN — ALPHA (runs FIRST)** | Throwaway inline quote proxies (spread-vol, imbalance-trend, staleness, intensity) added to the FULL trusted baseline; EXACT harness $-curve A/B at {2,5,10}% net of cost, vs baseline, dominating shuffle + predict-zero. GO = robust incremental net-$ (NOT a single-cut outlier — check per-day t, as the path-geom G0 did). | The binding constraint, screened before any build. |
| **G0b** | **CHEAP $-SCREEN — COST-TIMING (runs FIRST, in parallel)** | Predict realized next-window half-spread from the quote proxies (simple regression); measure (i) cross-sectional R²/IC of predicted-vs-realized cost, and (ii) the net-$ lift from charging PREDICTED per-name cost / gating entries to tight-spread moments vs the flat stub, on an already-discovered gross signal. GO = a material, robust net-$ improvement from cost-awareness. | The higher-leverage angle — shaving cost converts gross→net directly. |
| G1 | **Own-vol control (CRITICAL)** | Partial IC vs forward |move|/range residualizing feature+label on trailing sigma (+log-size) retains >= 60% of raw IC (collapse <0.40 = FAIL). | 10/13 prior survivors were re-priced vol-persistence. |
| G2 | **Incremental over FULL baseline + existing `quote_spread`** | Non-marginal gain-importance + surviving partial-IC in a model containing the full trusted baseline AND the existing static-quote group. | Net-new must be DYNAMICS/staleness/cost, not a re-stated spread snapshot. |
| G3 | **Shuffle baseline** | Real per-date IC > 99th pct of within-timestamp label-shuffle. | Leakage/overfit null. |
| G4 | **BY-FDR** | Survive Benjamini-Yekutieli q=0.10 across the family (reuse `quantlib.battery.family.benjamini_yekutieli`). | Multiple-testing honesty. |
| G5 | **Disjoint-window OOS replication** | Pass G1-G4 on window-1, replicate (sign + IC band + own-vol survival) on a held-out window. | swing_dc earned trust only via 9/9 disjoint replication. (Depth-limited; see §4 — may need the quote-depth backfill for a fully separate quarter.) |
| **G-STALE** | **Quote no-look-ahead + staleness (NAMED, quote-specific)** | Entry feature reads ONLY quotes with ts < T (µs); label reads ts >= T; stale-quote names flagged not silently used; asof backward-join never forward-fills across T. Automated bit-identical test (truncate-at-T == full-day for confirmed state). | Quotes make no-look-ahead HARDER (µs timing, staleness); this is the quote analogue of the zigzag-repaint guard. |
| G6 | **No-look-ahead bit-identical (general)** | Feature at T on frame truncated at T == feature at T on full frame, confirmed state. | Standard parity-by-construction. |
| G7 | **$-curve move (deploy gate, only if G0+G1-G6 pass)** | The PRODUCTION group improves the harness $-curve at conservative cuts vs baseline, dominating shuffle + predict-zero (framing a) AND/OR the cost-aware harness books materially more net-$ (framing b). | Moving the net-$ curve is what justifies a fingerprint change. |

**Decision rule.** Run **G0a + G0b FIRST**, report to Lead BEFORE any group/kernel. Build only if a G0 framing
shows robust (not single-day-outlier) incremental net-$; then G1-G6 + G-STALE on both windows; deploy only if
G7 green. Any FAIL → publish the null with the failing gate named. FIXED in advance: W ∈ {30,60}m, staleness
cap 5s, tau/lookbacks named here — sweeps enter the FDR count.

---

## 6. PRE-COMMITTED PIVOT — what a quote-tape null routes to
If BOTH G0 framings null on the current ~4k×42 window: that is a strong signal that at our scale, net-of-cost
cross-sectional edge is not extractable from the quote tape at THIS depth — route to (i) requesting the
quote-depth backfill (a deeper/broader tape may revive G5-replication or longer-lookback features that the
3mo window can't support), and (ii) re-examining whether the edge is HORIZON (multi-day, where the bar-only
weekly-reversal #205 was real-gross-but-median-negative-net) rather than substrate. State the depth the null
was reached at, so a deeper-tape re-test is a clean follow-up, not a dead end.

---

## 7. What this is NOT (scope guards)
- NOT a direction feature (framing a = magnitude; framing b = a cost observable).
- NOT a re-statement of the existing `quote_spread` static snapshot (G2 enforces dynamics/staleness/cost).
- NOT dependent on new data to START (G0 runs on the current ~4k×42 window); deeper quote backfill only
  strengthens G5 — flagged, not blocking.
- NOT built yet — G0a/G0b on throwaway proxies FIRST; no production group/kernel until a G0 framing is GO and
  the Lead reads this pre-reg. STOP here for the gate-read.
