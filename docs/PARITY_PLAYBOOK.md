# Parity Playbook & Data Timeline

The forward-looking, time-anchored plan: **what we capture when, when backfill becomes available,
and exactly which parity test each feature gets.** Every new feature is looked at through this lens
(§4). This playbook is always-on — referenced from `OPERATING_LOOP.md` and re-read when a feature
is proposed or when the daily schedule (§5) fires — so it survives a change of focus.

The north star it serves: a model trained on backfilled features runs live on identically-computed
real-time features with **zero train/serve skew** (FEATURE_PLATFORM.md §1.2). Parity is how we
*prove* that, and the proof is different for different data layers — especially sub-minute.

---

## 1. The daily data timeline (per trading day D, ET)

| Time (ET) | Event | What we have |
|---|---|---|
| **04:00** | Premarket capture ON | real-time tick + bar capture begins for D (extended hours) |
| **09:30** | Regular open | full-rate real-time stream |
| **16:00** | Regular close | (closing auction — excluded from some aggregates) |
| **20:00** | Extended hours end | **real-time capture for D is complete** |
| overnight | Exchanges/SIP settle D | corrections + cancels applied to the official tape |
| **D+1 ~08:30** | Backfill available | historical API serves the **settled** bars + ticks for D |
| **D+1 ~09:00** | **T+1 parity run** | recompute D from settled backfill, diff vs what we captured live |

The asymmetry that makes parity meaningful: **live is provisional** (the firehose hasn't seen
corrections), **the settled backfill is truth.** A feature is trustworthy only while live ≈ settled
within its declared tolerance.

---

## 2. Three data layers → three parity profiles

| Layer | Example features | Backfill source | Parity profile | Method |
|---|---|---|---|---|
| **A. Minute bars** | `ret_1m/5m/30m`, price levels | historical **bars** API | high (~99.5%) | exact cell, tight rel-tol (1e-6) |
| **B. Minute tick-aggregates** | `trade_freq_1m`, `signed_volume_1m` | historical **trades/quotes** → same agg code | medium; signing-sensitive | cell, per-feature rel-tol (e.g. 1% for signed volume) |
| **C. Sub-minute ticks** | burst count, `peak_trades_per_second`, inter-arrival, intra-minute acceleration | historical **trades/quotes** (raw) | **HARDEST** — see §3 | cell @ the minute it lands **+** the §3 rules **+** a distributional cross-check |

Measured to date (2026-06-12): Layer A 99.5–99.8% (T1/T2); Layer B `trade_freq` 99.5%,
`signed_volume_1m` 95.9% @1% tol (disposition in `LIFECYCLE_DEMOS.md`). Layer C is not yet built —
§3 is its plan.

---

## 3. Why sub-minute (Layer C) is harder — and the rules every sub-minute feature MUST obey

A sub-minute feature is computed from the precise sequence and timing of individual ticks inside a
minute. The real-time stream and the settled historical tick tape can disagree on that sequence in
ways minute bars never expose. The rules that make a sub-minute feature backfill-reproducible:

1. **Exchange-timestamp basis, never arrival/wall-clock.** Live ticks carry the exchange timestamp;
   features must key on it (not on our capture arrival time), or live and backfill key on different
   clocks and can never match. (Reinforces the no-`datetime.now()` rule.)
2. **No-drop capture is a PREREQUISITE, not a nicety.** A single dropped tick changes a burst
   statistic. **No sub-minute feature is certifiable until the FP1 no-drop gate is green** for its
   universe — you cannot have parity on a feature whose live inputs were lossy.
3. **Deterministic, reproducible ordering.** Within a timestamp, define a total order
   (e.g. `(exchange_ts, sequence_number)`) that the settled tape also yields — so tick-rule signing
   and sequence-sensitive stats reconstruct identically.
4. **Corrections: settled tape is truth; bound the residual.** A burst that included a later-busted
   print differs live vs settled. Prefer correction-robust formulations; otherwise declare a
   tolerance and bound the gap (as with `signed_volume_1m`).
5. **Identical condition handling.** Trade-condition / odd-lot filtering must be the same code live
   and backfill (it already is — shared `quantlib` aggregation).

**A sub-minute feature that cannot meet exact-cell parity under these rules gets a looser declared
tolerance AND a distributional cross-check** (compare the *distribution* of the statistic over the
day, not only exact cells) — and is flagged in the catalog as Layer C with its method. This is how
we keep them honest without pretending a firehose can byte-match a settled tape.

---

## 4. The per-feature parity lens (every new feature declares this — no exceptions)

Part of the certification checklist (FEATURE_PLATFORM.md §1.3). Every feature/group answers:
- **Data layer** — A (bars) / B (minute tick-agg) / C (sub-minute ticks).
- **Backfill source + availability** — which historical API, and when it settles (T+1).
- **Parity method** — exact / rel-tol _X_ / distributional, and the **per-tier floor** (≥95%).
- **Sub-minute rules (Layer C only)** — exchange-ts basis ✓, no-drop dependency ✓, order
  determinism ✓, correction handling stated.

Planned contract enhancement: add `layer` and `parity_method` fields to `FeatureSpec` so the lens
is **enforced** by the registry, not just documented — the catalog then shows every feature's layer
and parity method, and the daily run picks the right test automatically.

### 4.1 The trailing-buffer invariant (live-path correctness for windowed features)

The live capture path computes features over a **trailing in-memory buffer** of recent minutes, not
the whole day. For a windowed feature (any rolling/lagged Layer-A/B feature) to match the one-shot
backfill, the buffer MUST hold strictly more history than the feature consumes:

> **buffer_minutes ≥ max_feature_window + max_intra-feature_lag**

If the buffer equals the window, the buffer's leading-edge minute has no predecessor bar, so its
one-minute return (and anything derived from it) is **null live but defined in backfill** — the
long-window features silently diverge. This is a textbook instance of the trust-the-backfill failure
the platform exists to prevent, and it is invisible to a single-frame unit test; only a
**live-buffer-vs-backfill replay test** catches it. Such a test now exists
(`tests/test_fp_new_families.py::test_live_buffer_matches_backfill` plus an adversarial
`test_undersized_buffer_diverges`). Current longest minute window is `price_levels` 240m, so
`capture.DEFAULT_BUFFER_MINUTES = 300`. Whenever a feature with a window larger than ~290m is added,
this default MUST be raised in lockstep (until the buffer size is derived from the registry).

---

## 5. The standing schedule (runs regardless of what else we are focused on)

| When (ET) | Action | Owner |
|---|---|---|
| **03:55 daily** | Confirm real-time capture healthy for the premarket open: all shards subscribed, ingest lag OK, 0 drops | Prod |
| **continuous** | Capture (ticks + bars, all sessions); live-introspection sanity on the running vector | Prod / QA |
| **16:05 daily** | EOD: snapshot the live-captured day D as the parity reference | Prod |
| **~08:30 D+1** | Backfill the settled day D (bars + ticks) from the historical API | Prod |
| **~09:00 D+1** | **Run T+1 parity for D across ALL certified features.** Layer C features run the §3 tick-level variant. Any feature < its floor → **FAILING + owner**, excluded from scoring | QA |
| **weekly** | Parity-trend review + tolerance audit (are any declared tolerances drifting / unjustified?) | QA |

The **~09:00 D+1 T+1 parity run is the FP3 gate** — once FP1 capture is live, it becomes a
scheduled job whose dated report is committed daily (the 10-consecutive-green-days streak, FP_GOALS
G5). Until capture is live it runs on demand (`make parity DAY=…`).

---

## 6. How this stays visible even when focus moves
- `OPERATING_LOOP.md` points here as the standing data/parity schedule.
- The daily T+1 parity report is a committed artifact (anti-gaming §6) — a missing day is visible.
- This playbook is re-read at every feature proposal (the §4 lens) and every schedule fire (§5).
- Activation note: §5's automated cadence turns on with **FP1 live capture**; until then the
  manual `make parity` / `make introspect` cover it and the schedule is the target state.

---

## 7. Post-session correspondence & missing-data plan (the Monday → Tuesday check)

Every settled day runs **two independent checks, broken out by ET hour** so the early-morning and
after-hours sessions are visible separately:

1. **VALUE parity** — `make parity DAY=<D>`. Do the feature *values* match within tolerance? Built;
   per-feature, per-tier, per-method, ≥95% with the min-sample floor (§5).
2. **COVERAGE / missing-data** — `python -m quantlib.features.parity coverage <D>`. Do the same
   `(symbol, minute)` cells *exist* in both sources? Reports per ET hour:
   - `live_gaps` = cells the settled backfill has that we did NOT capture live → a **capture gap**.
   - `live_extra` = cells we captured that backfill lacks → over-capture / busted-trade / universe drift.
   - `live_coverage_pct` = 1 − gaps/backfill.

   These are distinct: value-parity says "the numbers we both have agree"; coverage says "did we
   even capture everything." You need both.

**Baseline findings (run 2026-06-13, pre-Monday):**
- A **partial-capture day** (Jun 10) shows **0% coverage before ~noon** — the check pinpoints exactly
  when the ingestor started. This is the canary for "did capture run all session."
- Even a **full day** (Jun 12) shows only **~80% live coverage vs backfill during RTH** (plus ~12k
  extra cells/hour). So stream and backfill do NOT cover the same ~20% of cells. **OPEN ACTION
  (before trusting live breadth Monday): root-cause the stream-vs-backfill universe/coverage gap**
  (likely universe-membership timing vs the backfill symbol set, or live bar drops).

**AM / PM collection nuances (must be handled, not assumed):**
- **Pre-market (04:00–09:30 ET):** the ingestor MUST be subscribed *before 04:00* or we lose the
  extended-hours open. Thin liquidity → low cell counts → expect more "INSUFFICIENT sample" tiers
  (min-sample floor), not failures. Alpaca historical covers extended-hours bars/trades/quotes.
- **After-hours (16:00–20:00 ET):** the **16:00 closing auction** is a special large print →
  excluded from signed-flow aggregates (the `signed_volume` close-tail finding, LIFECYCLE_DEMOS).
  Thinner; report per hour; don't treat thin-tail mismatches as failures.
- **Settlement timing:** backfill for day D is reliable ~D+1 morning; run both checks at ~09:00 D+1.

**Tuesday checklist for Monday's data (2026-06-16):**
- [ ] `python -m quantlib.features.parity coverage 2026-06-16` — per-hour live coverage; investigate
      any hour materially below the Jun-12 baseline, especially RTH and the pre-market open.
- [ ] `make parity DAY=2026-06-16` — per-feature/tier value parity ≥95% on sufficient samples.
- [ ] `materialize` both sources into the store; confirm `get_features(require_settled=True)` admits
      Monday once backfilled (and refuses it before).
