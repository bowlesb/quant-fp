# B4 — ADVERSARIAL AUDIT (try-to-kill)

**RAN 2026-06-19** by an adversarial verification agent. Mandate: find the reason B4 is NOT a real
harvestable edge. Three pre-named attacks (survivorship / harvestability / regime-concentration),
run with the UNMODIFIED harness (`quantlib.research.run_experiment`, `walk_forward_folds`, the
verbatim decile `long_short_backtest` logic) against the EXISTING deep-panel band NPZs in
`deep_data/bands/`. Read-only on the store; `--rm` sandboxes (`fp-ml`, 2-CPU cap, `PYTHONPATH=/app`,
repo mounted `:ro`); no container relaunch, no quantlib edit, no fingerprint/live touch. Scratch in
`/tmp/b4audit`.

## ⭐ VERDICT — **DOWNGRADED (not killed, but not capital-ready)**

B4 survives the *internal* statistical attacks (it is NOT regime-concentrated to a single year, and
it is the only band whose net edge survives a 2× cost stress) — but it fails the two attacks that
matter for **real harvestability**:

1. **SURVIVORSHIP is structural and UNMEASURABLE on this panel, and the exposure is maximal exactly
   in B4.** The deep backfill is the *current* universe carried back in time: 7368/7389 symbols
   (99.7%) have their last bar in 2026; only **21 symbols in the entire decade** ever disappear. A
   real ~7000-name small/mid-cap universe over 10 years loses *hundreds* of names to delisting,
   disproportionately downward. The delisted-down cohort — the names a short leg would have caught —
   is **absent from the data**, so the survivor-split test can't even find them (only 4 of 2000 B4
   names "disappear" in-panel). The inflation cannot be bounded from inside the panel; it can only
   be bounded *down*, and is plausibly large and in the edge's favor.
2. **The edge is harvestable ONLY in the one band where the cost estimate is least trustworthy.**
   Every *liquid, tradeable* band (B1/B2/B3) flips to a net LOSS at just **1.5× the optimistic CS
   cost**. B4 survives to 2.5× CS — but B4 is precisely where Corwin–Schultz most under-states the
   true next-open auction spread (ADV-min $2.2M). So "robust to cost" and "trustworthy cost" never
   co-occur across the bands.

**Net:** B4 is a real *in-sample* cross-sectional signal, but the "RESURRECTED/HARDENED-CONFIRM,
net Sharpe +1.81" headline overstates a tradeable edge. The two independent biases (absent
delisters + CS understatement) both push the *measured* edge **above** the harvestable one, and
both are concentrated in B4's illiquid tail. **Do NOT size capital on the CS-cost number.** The
binding gate remains the REAL next-open execution-cost measurement, and that measurement must be run
on a universe that *includes delisted names* (or the result discounted heavily for their absence).

---

## ATTACK 1 — SURVIVORSHIP (the prime suspect) → **CONFIRMED, structural, B4-maximal**

### (a) Per-year coverage: the universe grows monotonically = the survivorship signature

Active symbols (valid RTH close) per year in the deep daily panel:

| year | active syms | B4-band distinct syms present |
|-----:|------------:|------------------------------:|
| 2016 | 3271 | 994  (of the 2000-name band) |
| 2017 | 3478 | 1067 |
| 2018 | 3703 | 1173 |
| 2019 | 3958 | 1242 |
| 2020 | 4370 | 1384 |
| 2022 | 5440 | 1727 |
| 2023 | 5650 | 1765 |
| 2024 | 6056 | 1873 |
| 2025 | 6796 | 1965 |
| 2026 | 7151 | 1886 |

A real point-in-time universe **churns** (IPOs in AND delistings out) — the count fluctuates. This
count rises **monotonically** 3271→7151. That is the fingerprint of "current universe carried back":
names that exist *today* are traced back as far as they were listed; names that *died* are gone. In
2016 the 2000-name B4 band is filled by only **994** symbols that survived to today — the early
B4 cross-section is forward-selected.

### (b) Survivor-split re-run (last-bar-within-5d-of-2026-06-18 = survivor)

| subset | rows | OOS IC | NW t | net Sharpe |
|--------|-----:|-------:|-----:|-----------:|
| FULL | 1,128,482 | 0.05694 | 13.05 | +1.81 |
| SURVIVORS_ONLY | 1,127,395 | 0.05637 | 12.99 | +2.01 |
| DISAPPEARED_ONLY | 1,087 | nan | nan | −3.49 |

The IC barely moves when "disappeared" names are removed — **because there are almost none to
remove** (4 of 2000 symbols, 0.10% of rows). This is NOT evidence the edge is survivorship-free; it
is evidence the panel **cannot test for survivorship at all**: the delisted-out cohort was never
backfilled, so the very names that would shrink the edge are absent from the dataset. The last-seen
proxy can only catch names that were ingested and then stopped — and the data shows that essentially
never happens here (see below).

### (c) Direction-vs-delisting

- Last-seen-year distribution across all 7389 symbols: **2026 → 7368, 2025 → 17, 2024 → 1,
  2023 → 2, 2019 → 1.** Twenty-one disappearances in a decade. (A realistic small-cap universe loses
  ~4–8%/yr → on the order of *thousands* of cumulative delistings over 2016–2026.)
- The few in-panel "disappeared" B4 names DO show the expected terminal downward drift: in their
  last 60 days, realized fwd_1d is **60% negative, median −0.286%** — i.e. dying names do drift down
  into a short leg, consistent with the bias mechanism. But n=60 rows / 4 names is far too small to
  drive the edge; the *real* down-delisters are simply missing.

**Magnitude of plausible inflation:** unquantifiable from inside the panel (its defining flaw), but
**one-directional and largest in B4**. Small-caps delist at the highest rate and most asymmetrically
downward; B4/B5 are the small-cap bands. With the down-delisters absent, both legs are computed on
"names that made it," which mechanically inflates a cross-sectional reversal/relative-value signal
in the illiquid tail. Treat the +1.81 Sharpe as an **upper bound**, not an estimate.

---

## ATTACK 2 — HARVESTABILITY / band-monotonicity trap → **CONFIRMED untradeable-tail**

Cost faithfulness re-confirmed: `long_short_backtest` charges `cost_bps_oneway` on the realized
**turnover** (sum of |Δweight| across BOTH legs) every period; with nightly full re-rank
turnover ≈3.33 this is ≈ the full round-trip on both legs at the next-open. CS half-spread is the
cost input — the very number this attack stresses.

Net Sharpe at escalating one-way cost (1×/1.5×/2×/2.5× each band's OWN CS-derived cost):

| band | own bps | 1.0× | 1.5× | 2.0× | 2.5× | flips to loss at |
|------|--------:|-----:|-----:|-----:|-----:|:----------------:|
| B1 0001-0500 (liquid) | 7.47 | **+0.74** | −0.20 | −1.14 | −2.08 | **1.5×** |
| B2 0500-1000 | 8.52 | +0.30 | −0.23 | −0.76 | −1.30 | **1.5×** |
| B3 1000-2000 | 12.90 | +0.01 | −0.59 | −1.18 | −1.77 | **~1.0×** |
| **B4 2000-4000** | 18.57 | **+1.81** | +1.22 | +0.62 | +0.03 | **~2.5×** |
| B5 4000-6000 | 18.55 | +0.43 | −0.29 | −1.00 | −1.72 | **1.5×** |

**The liquid bands have essentially zero cost headroom.** B1 — the most tradeable, where CS is most
trustworthy — dies at 1.5× CS. There is **no net-positive edge left in liquid names after even a
modestly realistic cost.** Only B4 survives a 2× stress, and B4 is exactly the band where the real
next-open auction spread is most likely 1.5–2.5× the CS estimate (ADV-min $2.24M, median $7.2M;
the verdict itself flags "CS under-states next-open auction spreads on small-caps"). The edge lives
where it can't be traded *and* where its own cost can't be trusted.

---

## ATTACK 3 — REGIME / YEAR CONCENTRATION → **edge is NOT single-year-concentrated (B4's best result)**

By-year net-of-own-cost L/S (reproduced verbatim from the harness OOS predictions):

| year | days | mean_net | sum_net | year Sharpe | NW t |
|-----:|----:|---------:|--------:|-----------:|-----:|
| 2018 | 166 | +0.00603 | +1.00 | 0.96 | 0.67 |
| 2019 | 206 | −0.00227 | −0.47 | −0.54 | −0.53 |
| 2020 | 250 | +0.01385 | +3.46 | 2.44 | 2.50 |
| 2021 | 251 | +0.00927 | +2.33 | 2.09 | 1.83 |
| 2022 | 250 | +0.00302 | +0.75 | 0.56 | 0.65 |
| 2023 | 249 | +0.00991 | +2.47 | 1.60 | 1.56 |
| 2024 | 252 | +0.02106 | +5.31 | 3.68 | 3.30 |
| 2025 | 249 | +0.01106 | +2.75 | 2.83 | 2.49 |
| 2026* | 114 | +0.00987 | +1.13 | 2.77 | 1.83 |

- **Concentration:** top year (2024) = **30.1%** of total cumulative net P&L; top-2 (2024+2020) =
  **49.8%**. Concentrated, but NOT a single-year artifact, and the t=13 is genuinely earned across 8
  years (7/8 positive), not by one regime.
- **Tradeable in calm years too:** 2021 (Sharpe 2.09), 2023 (1.60), 2025 (2.83) are positive — the
  premium is not exclusively a 2020-crash / high-VIX phenomenon. This is the **inverse** of the
  Lane D EW-basket near-null (carried wholly by 2025-H2, NW|t|=1.75).
- **Reconciling t=13 (xsec) vs Lane D near-null (EW basket):** they are different estimands. Lane D
  measured a *directional EW overnight basket* (a beta/size tilt); B4 measures a *within-day
  cross-sectional decile L/S* (dollar-neutral relative value). The cross-section can be strongly
  predictable even when the basket's average overnight return is flat — no contradiction, and it
  means the t=13 is not just the Lane D tilt re-labelled.
- **Contrast — liquid B1 by-year:** full-sample net Sharpe only **0.74**, top-2 years = **89.4%**
  of P&L, with **two significantly-negative years** (2019 NWt −2.02, 2026 −1.71/Sharpe −2.20). The
  liquid, tradeable version of the same signal is weak AND unstable — reinforcing Attack 2.

**Attack 3 verdict:** B4 PASSES the regime attack on its own terms (persistent across years,
positive in calm years, not one-regime). This is the one attack B4 genuinely survives.

---

## SUMMARY SCORECARD

| attack | result | bearing on capital |
|--------|--------|--------------------|
| 1 Survivorship | **CONFIRMED structural, unmeasurable, B4-maximal** | edge is an UPPER bound; down-delisters absent |
| 2 Harvestability | **CONFIRMED**: liquid bands die at 1.5× CS; edge survives only in untradeable/untrustworthy-cost B4 tail | the +1.81 rests on the least-reliable cost |
| 3 Regime | **B4 SURVIVES** (persistent, not single-year, positive in calm years) | the one genuinely robust property |

**ONE-LINE VERDICT:** **DOWNGRADED** — B4 is a real, persistent in-sample cross-sectional signal
(it survives the regime attack), but it is NOT yet a demonstrated harvestable edge: it is
survivorship-inflated by construction (down-delisters absent, exposure maximal in the small-cap
band) and it survives cost only in the one band where the cost estimate is least trustworthy, while
every liquid/tradeable band collapses at 1.5× CS. Capital remains gated on a REAL next-open
execution-cost measurement run on a delisting-inclusive universe — the CS +1.81 Sharpe is an
optimistic ceiling, not a tradeable number.

### Reproducibility
- Scripts: `/tmp/b4audit/{survivor_split,cost_stress_bands,byyear_concentration}.py`
- Inputs: `deep_data/bands/band_B{1..5}_*_fwd_1d.npz` + `band_costs_deep.json` (unchanged);
  `deep_data/overnight_daily_deep.parquet` for the last-seen/listing proxy.
- Harness: UNMODIFIED `quantlib.research.run_experiment` / `walk_forward_folds` / decile
  `long_short_backtest`. `fp-ml` `--rm`, 2-CPU, `PYTHONPATH=/app`, repo + store mounted `:ro`. No
  quantlib edit, no live tree, no fingerprint, no deploy, no secrets.
