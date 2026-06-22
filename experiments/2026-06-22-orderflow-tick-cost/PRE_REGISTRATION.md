# PRE-REGISTRATION — Order-flow / tick-tape hunt (signed-flow ALPHA screen + tick-effective-spread COST upgrade)

**Date:** 2026-06-22  **Author:** EdgeHuntDriver (Lead-assigned)  **Status:** ONE-CALL-READY (data IS landed — see §4)
**Code SHA:** `d3dac1eb8e4344981345495afb32374293cb7301` (origin/main at design time)
**Lane:** deep TICK TAPE microstructure — the raw trade tape (signed flow + realized effective spread), the one
substrate the bar-derived `trade_flow`/`signed_trade_ratio` groups CANNOT see (they read minute-aggregated
tick-rule signed volume; this reads per-print price/size/exchange/conditions and the OVERLAPPING NBBO).

This is the order-flow experiment the Lead asked to be "one-call-ready the moment the backfill lands." The
central design finding is that **the backfill is already landed** (§4): the gate the assignment assumed
(Ben-budget order-flow backfill) is stale. So this pre-reg is RUNNABLE NOW via `run_screen.py`, not a
placeholder waiting on data. It carries forward every methodology gate earned over 14+ hunts and leads with
the G0 cheap $-screen.

---

## 0. Protocol carried forward (standing methodology)
- **G0-FIRST.** Lead with a cheap THROWAWAY-PROXY $-screen on the binding constraint (incremental net-$ over
  the FULL trusted baseline + the EXISTING order-flow groups) BEFORE any production feature/kernel.
- **The binding constraint is NET-OF-COST**, every time. Every settled null (11 direction, order-flow 0/4,
  quote-tape G0a, HF03 spread-capture KILL) was real gross and died after spread/impact/adverse-selection.
- Median-anchored, $1 floor, tradeable entry ≥09:35 ET, own-vol control as THE IC gate, BY-FDR, disjoint-window
  OOS, no-look-ahead bit-identical — unchanged.
- **A null is a publishable answer.** State the depth/window the null was reached at.

---

## 1. PRIOR-ART HONESTY (what is already settled — this hunt is scoped to NOT re-run it)
The order-flow / quote-microstructure axis is heavily worked. The settled negatives, with their substrate:

| Prior experiment | Substrate | Verdict |
|---|---|---|
| `2026-06-16-hf01-quote-imbalance` | NBBO quote imbalance | null |
| `2026-06-16-hf03-spread-capture` | post-at-touch spread capture | **KILL** — adverse selection + queue position eat the spread; positive only under optimistic fill |
| `2026-06-16-signed-trade-ratio` | minute-agg tick-rule signed volume | engineering primitive shipped; edge DEFERRED (same liquid-tier cost wall) |
| order-flow verdict (memory) | bar-derived OFI / trade-freq / book-depth | **0/4** — intensity not alpha; both cross-sectional surfaces settled |
| `2026-06-21-quote-tape` G0a | quote-DYNAMICS (spread vol/trend, imbalance trend, staleness, intensity) | **NO-GO** — gross ranking improves, net-$ falls at every conservative cut |
| `2026-06-21-quote-tape` G0b | quote proxies → realized **quoted** half-spread | **KEEPER** — OOS R²=0.575, rank-IC=0.902; wired into panel as `_attach_realized_half_spread` (Stage-1 truth) |

**What this hunt does NOT re-run:** the quote-dynamics ALPHA G0a (settled NO-GO), the bar-derived OFI family
(settled 0/4), passive-post spread capture (HF03 KILL). Re-running those would be wasted compute.

**The two genuinely-unfilled gaps this hunt targets — both newly enabled by the LANDED TICK TAPE:**
1. **(ALPHA, screen-and-likely-null)** Tick-level signed-flow features the minute-agg groups CANNOT compute:
   per-print Lee-Ready classification (trade price vs the contemporaneous NBBO mid, not the minute tick-rule),
   order-flow imbalance in *notional* (Σ signed price·size), large-print / block signed flow (institutional
   footprint), and intra-minute flow *persistence*. The prior 0/4 OFI null used minute-agg tick-rule signed
   volume — this is a strictly finer signal. Screened at G0; the prior pattern predicts a null, but the finer
   substrate has never been screened, so the screen is honest and cheap.
2. **(COST, the high-leverage KEEPER continuation)** Upgrade the cost model from the **quoted** half-spread
   (G0b, already wired) to the **realized effective spread** measured from the OVERLAPPING trade+quote tapes:
   per-execution signed `(trade_price − mid) / mid`, time/volume-weighted over the forward window. The quoted
   spread is what the book SHOWS; the effective spread is what trades actually PAY (often < quoted inside the
   spread for marketable-limit fills, > quoted under impact for sweeps). This is the "next-fidelity upgrade"
   the quote-tape pre-reg §2 explicitly flagged and is the single highest-leverage deliverable available —
   it sharpens EVERY past and future net-of-cost gate, exactly as G0b's quoted model did (an 18% optimism
   haircut) but with the true paid cost rather than the displayed cost.

---

## 2. The thesis — TWO framings, both screened at G0

### (a) ALPHA framing — tick-level signed flow predicts forward MAGNITUDE/return net of cost
**Claim.** Per-print order-flow observables carry net-new cross-sectional information about the forward 30m
move that the minute-aggregated `trade_flow`/`signed_trade_ratio` groups (which already nulled at 0/4) cannot
see, because they discard within-minute print structure and use the crude minute tick-rule rather than
quote-anchored Lee-Ready classification. Concretely, over the trailing window `[T−W, T)`:
- **Lee-Ready signed notional imbalance** = Σ sign·(price·size) / Σ(price·size), where sign is +1 if the
  print is above the contemporaneous NBBO mid (asof-backward quote), −1 if below, tick-rule tiebreak at mid.
  (The minute-agg `signed_volume` uses the tick rule on minute bars; this uses the QUOTE-anchored rule per
  print — the textbook-correct classifier.)
- **Block / large-print signed flow** = the same imbalance restricted to prints ≥ the name's trailing
  size-p95 (the institutional footprint the odd/round-lot `trade_size_dist` fractions can't sign).
- **Flow persistence** = autocorrelation / run-length of signed notional across consecutive prints (is the
  flow one-sided and sustained, vs balanced churn) — distinct from `tick_runlength`'s minute-bounded run.
- **VWAP-deviation of own flow** = signed (execution VWAP − interval mid) — am I being filled adversely.

**Direction discipline.** This is screened as MAGNITUDE/risk (predict forward |excess move| and signed-excess
both, reported separately); the 11 direction nulls mean signed alpha gets a HIGHER bar and is reported but not
chased on a marginal result.

### (b) COST framing — realized EFFECTIVE spread (the higher-leverage KEEPER continuation)
**Claim (worth more right now).** From the overlapping trade+quote tapes, the realized effective half-spread a
name actually PAYS over `[T, T+h]` — `effective_half_spread = | Σ |price − mid|·size / Σ size |` annualized to
bps, and the signed version (adverse vs favorable) — is (i) measurable directly (the cleanest possible label),
(ii) better-predicted from trailing tick features than from the trailing QUOTED spread alone (G2 vs G0b's
quoted model), and (iii) materially different from the quoted half-spread already wired — so replacing/blending
it into `long_short_per_name_cost` makes every net-of-cost verdict TRUE-cost rather than displayed-cost. A
feature that sharpens realized cost converts already-discovered gross alpha into correctly-graded net $,
directly attacking the binding constraint — the same logic that made G0b a KEEPER even though G0a nulled.

---

## 3. Construction (point-in-time, tick-staleness-safe, parity-portable)

Per name at entry instant T (tradeable ≥09:35 ET), from the raw trade tape `[T−W, T)` (W ∈ {30, 60}m) joined
asof-backward to the NBBO quote tape (last quote with `ts < print_ts`, strict):

- **Lee-Ready sign** per print: `+1` if `price > mid`, `−1` if `price < mid`, else tick-rule (sign of last
  non-zero price change) — the quote anchor is the asof-backward NBBO (no forward leak).
- **Signed notional imbalance**, **block signed flow** (size ≥ trailing p95), **flow persistence**
  (signed-print autocorr / run-length), **execution VWAP-deviation** — all §2(a).
- **Realized effective half-spread label** (§2(b)): over `[T, T+h]`, size-weighted `|price − mid|/mid·1e4` and
  its signed (post-fill drift) decomposition.
- **Condition-code hygiene:** exclude non-eligible prints (use the `conditions` column — odd-lot/derivatively-
  priced/out-of-sequence flags must not pollute the signed-flow or the effective-spread label). The exact
  excluded condition set is FIXED in `run_screen.py` (`EXCLUDED_CONDITIONS`) and entered into the FDR count.

**TICK no-look-ahead (G-STALE, quote-specific, enforced):**
- Entry features read ONLY prints/quotes with `ts < T` strict (µs); the effective-spread label reads `ts ≥ T`.
- Each print's sign is anchored to the asof-backward NBBO (`ts ≤ print_ts`), NEVER a future quote.
- A name whose newest print/quote at T is older than a staleness cap (5s, FIXED) is FLAGGED, not silently used.
- Automated bit-identical test: feature at T on the tape truncated at T == feature at T on the full-day tape.

---

## 4. DATA-STATE — VERIFIED LANDED (the central finding; correct the stale "not landed" premise)

Read-only survey of the real store (docker named volume `fp_store_real`, mount `-v fp_store_real:/store:ro`),
2026-06-22:

| layer | broad breadth begins | broad syms/day | head depth | head syms |
|---|---|---|---|---|
| `raw/trades` | **2026-03-30** | **~7,608** | back to ~2024-12 | ~855 |
| `raw/quotes` | **2026-03-30** | **~4,042** | back to ~2024-12 | ~542 |
| `raw/bars` | full 18mo | ~7,690 | — | — |

- Trade-tape schema (verified on AAPL 2026-06-10): `symbol, ts(µs-UTC), price, size, exchange, conditions,
  tape, trade_id` — **993,586 prints that day**. Quote schema: `symbol, ts, bid_price, bid_size, bid_exchange,
  ask_price, ask_size, ask_exchange, conditions, tape` — 1,751,410 quotes that day. Both tapes OVERLAP →
  tick-level Lee-Ready signing and realized effective spread are computable NOW.
- **The broad trade+quote window is ~55 trading days, 2026-03-30 .. 2026-06-18.** This is the SAME well-covered
  cross-sectional window the quote-tape G0 and path-geometry G0 used → the screen is feasible immediately.
- The "trades thin ~2k/day" prior (memory `project-deep-raw-history-foundation`) is **STALE** — broad trades
  are ~7.6k/day. Memory should be updated.

**What runs NOW vs what needs the §6 backfill:** the entire G0a + G0b screen and G1–G4 + G-STALE + G6 run on
the landed ~7.6k×55d window. Only **G5 (genuinely-disjoint-quarter OOS replication)** needs the breadth-back
extension (§6), because the broad tape is a single ~3-month regime; G5 is the ONLY data-gated gate.

---

## 5. The gate — PRE-COMMITTED pass/fail (G0 first; G1–G6 only if G0 GO)

Substrate: ~7.6k-sym × ~55-date window (2026-03-30..06-18), forward-30m cross-sectional EXCESS label (a) /
realized effective half-spread label (b), $1 floor, tradeable entry ≥09:35 ET, two disjoint windows.

| # | Gate | Pass bar | Why |
|---|------|----------|-----|
| **G0a** | **CHEAP $-SCREEN — ALPHA (runs FIRST)** | Throwaway tick signed-flow proxies (Lee-Ready notional imbalance, block flow, persistence) added to the FULL trusted baseline + `trade_flow` + `signed_trade_ratio` + `quote_spread`; EXACT harness $-curve A/B at {2,5,10}% net of cost, vs baseline, dominating shuffle + predict-zero. GO = robust incremental net-$ (NOT a single-cut outlier — per-day t check). | The binding constraint, screened before any build. Prior 0/4 predicts null; the finer substrate earns one honest screen. |
| **G0b** | **CHEAP $-SCREEN — EFFECTIVE-COST MODEL (runs FIRST, parallel)** | Predict realized next-window EFFECTIVE half-spread from trailing tick features; report (i) OOS R²/rank-IC of predicted-vs-realized effective cost, (ii) its INCREMENTAL accuracy over the already-wired QUOTED-spread model (must beat G0b-quoted, not just the flat stub), (iii) the net-$ delta from charging effective vs quoted cost on an already-discovered gross signal. GO = effective cost is materially different from quoted AND better-predicted → wire it. | The higher-leverage angle; the KEEPER continuation. Cost-accuracy paid off once (G0b-quoted); effective is the true paid cost. |
| G1 | **Own-vol control (CRITICAL)** | Partial IC vs forward |move|/range residualizing feature+label on trailing sigma (+log-size) retains ≥60% of raw IC (collapse <0.40 = FAIL). | 10/13 prior survivors were re-priced vol-persistence. |
| G2 | **Incremental over FULL baseline + ALL existing order-flow/quote groups** | Non-marginal gain-importance + surviving partial-IC in a model containing the full trusted baseline AND `trade_flow`+`signed_trade_ratio`+`trade_freq_z`+`trade_size_dist`+`microstructure_burst`+`tick_runlength`+`quote_spread`. | Net-new must be tick-level Lee-Ready/notional/block/persistence, not a re-stated minute-agg signed volume. |
| G3 | **Shuffle baseline** | Real per-date IC > 99th pct of within-timestamp label-shuffle. | Leakage/overfit null. |
| G4 | **BY-FDR** | Survive Benjamini-Yekutieli q=0.10 across the family (`quantlib.battery.family.benjamini_yekutieli`). | Multiple-testing honesty. |
| G5 | **Disjoint-window OOS replication** | Pass G1–G4 on window-1, replicate (sign + IC band + own-vol survival) on a genuinely separate window. | The ONLY data-gated gate — needs the §6 breadth-back extension for a separate QUARTER (current broad tape is one ~3mo regime). |
| **G-STALE** | **Tick no-look-ahead + staleness (NAMED)** | Entry reads ONLY ts<T (µs); sign anchored asof-backward NBBO (ts≤print_ts); label reads ts≥T; stale names flagged; bit-identical truncate-at-T test. | Tick µs timing + the trade/quote asof-join make leak HARDER; this is the tick analogue of the zigzag-repaint guard. |
| G6 | **No-look-ahead bit-identical (general)** | Feature at T on frame truncated at T == feature at T on full frame, confirmed state. | Parity-by-construction. |
| G7 | **$-curve move (deploy gate, only if G0+G1–G6 pass)** | The PRODUCTION group improves the harness $-curve at conservative cuts vs baseline, dominating shuffle + predict-zero (a); AND/OR the effective-cost model books materially more-accurate net-$ in `long_short_per_name_cost` (b). | Moving the net-$ curve / true-cost grading is what justifies a fingerprint change. |

**Decision rule.** Run **G0a + G0b FIRST** (`run_screen.py`), report to Lead BEFORE any group/kernel. Build
only if a G0 framing shows robust (not single-day-outlier) incremental net-$ / a materially-better effective
cost model; then G1–G6 + G-STALE on both windows; deploy only if G7 green. Any FAIL → publish the null with
the failing gate named. FIXED in advance: W ∈ {30,60}m, staleness cap 5s, block threshold = trailing size-p95,
`EXCLUDED_CONDITIONS` set in `run_screen.py` — all sweeps enter the FDR count.

---

## 6. BACKFILL-PRIORITY RECOMMENDATION (the precise ask for Ben's budget decision)

**The order-flow backfill is NOT the gate** — the broad trade+quote tape is landed (§4). The ONLY genuinely
data-gated item is **G5 disjoint-quarter OOS replication**, and only IF G0 goes GO (prior art predicts a
G0a null; G0b-effective likely GO as infrastructure). So the recommendation is conditional and SMALL:

**Recommendation (priority order):**
1. **NOTHING for G0/G1–G4/G-STALE/G6** — they run now on the landed window. Run the screen first; spend $0.
2. **IF (and only if) a G0 framing goes GO:** extend the **trade+quote BREADTH backward from 2026-03-30 to
   2026-01-01** (one prior quarter), ideally 2025-10-01 (two quarters), so G5's held-out window is a separate
   regime, not a within-quarter split. This is a **breadth-at-depth** fill (the ~6,750 non-head trade names /
   ~3,500 non-head quote names that are absent before 2026-03-30) — NOT new head dates (head names already
   reach 2024-12).
3. **For the COST model (G0b-effective) specifically:** the same breadth-back extension strengthens its
   cross-regime robustness, but it is NOT required to ship the effective-cost upgrade — that can wire on the
   current window exactly as the quoted G0b model did.

**Rough cost (Alpaca, $0 marginal data per memory `feedback-alpaca-first-and-crypto-canary` — unlimited
access; cost is compute + storage + run-time, manifest dedup means no re-download of existing):** one extra
quarter of broad trades ≈ 6,750 syms × ~63 trading days, at ~1M prints/day for liquid names (far fewer for the
long tail). Two quarters ≈ 2×. This is a bounded, Lead-budgeted, `quant-backfill`-named (mem
`reference-backfill-memguard-name`) staged fill — NOT a large speculative download. The precise byte/row
estimate should be produced by a `--dry-run` manifest-diff before any fetch.

**Net steer:** run the screen on the landed data FIRST (free); only ask Ben for the one-quarter breadth-back
extension if a G0 framing earns a G5, OR proactively for the effective-cost model's robustness if the Lead
wants cross-regime cost validation. Do NOT run a large backfill speculatively.

---

## 7. What this is NOT (scope guards)
- NOT a re-run of the settled quote-dynamics ALPHA G0a, the bar-derived OFI 0/4, or HF03 spread-capture.
- NOT a direction feature chase (magnitude-first; signed reported at a higher bar).
- NOT a re-statement of the minute-agg `signed_volume` (G2 enforces tick-level Lee-Ready/notional/block).
- NOT data-gated to START — G0+most gates run on the landed ~7.6k×55d window; only G5 needs §6.
- NOT built as a production group yet — G0a/G0b on throwaway proxies FIRST; no group/kernel until a G0 framing
  is GO and the Lead reads this pre-reg.
