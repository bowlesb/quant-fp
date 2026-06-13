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
