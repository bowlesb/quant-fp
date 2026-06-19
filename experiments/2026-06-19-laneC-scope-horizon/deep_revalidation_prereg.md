# Lane C B4 — DEEP-PANEL RE-VALIDATION: PRE-REGISTRATION (design only, ARMED — not run)

STAMPED **2026-06-19** by Modeller/B4-deep-prereg. **DESIGN ONLY — no data run, no quantlib edit.**
HEAD `52394f0b5a9c8d927550fddfedef857d9d9b2a2c` @ `main`.

**Data state at stamp (why this is pre-registration, not a result):** the deep-bars backfill
(2016-01-04 → 2026-06-16, full universe 7631 syms, launched 2026-06-19T11:55Z) is **filling
alphabetically, ~936-1618 / 7631 symbols** (≈12-21% of the universe, ~11-16h to full at stamp rate).
The rank-2000-3787 small-cap B4 cohort does **NOT** yet have representative deep coverage — the
alphabetical partial fill is a biased slice (A-* tickers), not the cross-section B4 needs. This doc
is written now and frozen so the test fires the instant the panel is ready, with **zero** post-hoc
threshold latitude.

Reused harness (UNMODIFIED when fired): `quantlib.research.run_experiment` +
`quantlib.backtest.long_short_backtest`. Reused construction: `build_overnight_dataset.py` /
`build_bands.py` (the SAME 13 EOD features, $1 floor, per-day winsor, 1d close→next-open label,
Corwin–Schultz cost). Canonical universe source: the #149 `/api/liquidity-bands` surface
(`services/dashboard/liquidity_bands.py`), band cut `2000-4000` (== adjudicated B4; terminates at the
deepest ranked symbol per year). NO fingerprint, NO live-tree edit, NO deploy.

---

## WHY THIS TEST — THE TOM PRECEDENT (the kill-shot template)

The ONLY surviving return-signal lead in the entire research portfolio is **Lane C B4**: the small-cap
(ADV rank ~2000-3787, median ADV $5.8M) overnight close→next-open directional premium. On the 18-month
panel (357 OOS days) B4 cleared every pre-registered net-of-cost leg:

| | 18mo B4 result |
|---|---|
| OOS rank-IC | **0.0318** |
| NW \|t\| (lag=3) | **3.71** |
| gross breakeven (1-way) | **36.35 bps** |
| own cost (median CS½ + 1bp, 1-way) | **20.87 bps** |
| net Sharpe @ own cost | **+1.33** |
| executable at $100K | yes (per-name ≈$770 ≈0.3% of min ADV) |

**BUT B4 is knife-edge and period-dependent**: split-half showed the edge present in both halves but
neither half independently clears \|t\|≥2.0 (the t=3.71 is pooled), the first half's gross breakeven is
*negative*, and economic viability leans on 2025-H2. Cost sensitivity: B4 flips to a net LOSS at
~1.7-2× the CS cost estimate, and CS is known to under-state next-open auction spreads on exactly this
illiquid cohort.

**CRITICAL PRECEDENT — Turn-of-Month.** TOM was ALSO an 18mo near-miss (AAPL NW \|t\|=2.17). On the deep
~10yr panel (the pre-registered resolver, ~7× the power) it **DIED DECISIVELY**: the effect *collapsed*
rather than strengthened (AAPL \|t\| 2.17→1.35, Sharpe 1.66→0.49; 5 deep mega-caps all \|t\|<1.4, shuffle
p>0.18) and the **by-year spread SIGN-FLIPPED** — it was small-sample upper-tail noise. The frozen gate
was imported byte-identical; deep history killed a noise lead *before any capital*.

**B4 is at the same structural risk** (an 18mo near-miss leaning on one half-window). The deep
2016-2026 panel (~120 turn-of-months, ~10yr) is the power upgrade that settles whether the B4 overnight
premium is a **persistent tradeable effect** or **18mo noise** — BEFORE Ben commits paper/real capital.
This de-risks the B4 paper-trade decision.

---

## HYPOTHESIS

The small-cap (ADV rank 2000-3787, 60-day-stable cohort) overnight close→next-open directional premium
is a **PERSISTENT, tradeable effect that survives realistic small-cap round-trip cost across the full
~10yr panel** — NOT an 18-month small-sample artifact.

Directional prediction (declared before results): if real, the deep-panel B4 IC/t **hold or strengthen**
with the added power AND are **positive in a clear majority of individual years with no sign-flip**.
If it is 18mo noise (the TOM outcome), the pooled \|t\| **collapses** and the by-year table **sign-flips**.

---

## FROZEN CRITERIA — the EXACT 18mo B4 gate (NO relaxation) + a persistence requirement

All five legs are imported **byte-identical** from `boundary_hypothesis.md` (the 18mo B4 gate). Headline
horizon = **1d** (Lane C's headline); 2d/3d are descriptive by-horizon only, NOT additional shots.
Universe = the single band **ADV rank 2000-4000** (== B4; terminates at the deepest ranked name per
year). Run through the UNMODIFIED harness on the FULL ~10yr deep panel.

**TRADEABLE-PERSISTENT iff the B4 band clears ALL of A1–A5 AND the persistence requirement P:**

- **A1. OOS rank-IC ≥ 0.01**
- **A2. NW \|t\| ≥ 2.0** (lag=3 conservative, on the full ~10yr OOS day series)
- **A3. net-of-OWN-cost breakeven > 0**: the band's harness `breakeven_cost_bps` (one-way) **>** that
  band's own one-way cost = `median Corwin–Schultz high-low half-spread (bps) + 1.0`, computed
  point-in-time on the deep panel (the cost is recomputed on deep data, NOT carried over from 18mo).
- **A4. positive NET Sharpe** at the band's own cost (re-run the L/S with `cost_bps_oneway = own cost`;
  `sharpe_net > 0`).
- **A5. executable at $100K**: per-name notional ≪ band ADV (informational at this size; verified, not
  the binding leg — spread is the binding cost, as on 18mo).

**P. PERSISTENCE (the leg TOM FAILED — stated in full BEFORE any result):**
Split the OOS day series into individual **calendar years** (2016, 2017, …, through the deep panel's
final partial year). For each year compute the B4 1d L/S **net-of-own-cost mean daily P&L** (sign of
the realized edge that year). Require BOTH:

  - **P1 (majority-positive):** the net edge is **POSITIVE in ≥ 7 of the 10 full years** (2016-2025;
    the 2026 partial stub is reported but excluded from the 7/10 count to avoid a fractional year). If
    the panel yields fewer than 10 full years, the threshold is **≥ ⌈0.70 × N_full_years⌉** positive,
    fixed by this formula (no judgment call after seeing N).
  - **P2 (no decisive sign-flip):** **no individual full year** has a net edge that is both NEGATIVE and
    statistically distinguishable from zero at that year's own NW \|t\| ≥ 2.0. A single noisy-negative
    year (\|t\|<2) is tolerated; a *significant* negative year is a kill (it means the premium reverses,
    not merely weakens — the TOM sign-flip signature).

The **by-year net-edge table is the kill-shot / confirm-shot** and MUST be produced and inspected as
the primary deliverable of the run, exactly as it was for TOM.

**VERDICT (fixed before results):**
- **PERSISTENT TRADEABLE NICHE** iff A1–A5 AND P1 AND P2 all hold → B4 is a deep-history-confirmed
  small-cap overnight L/S → motivates a paper-book allocation proposal to Lead/Ben (still gated on the
  separately-required REAL next-open execution-cost measurement from `results_overnight_boundary.md`).
- **DEAD / 18mo-NOISE (the TOM outcome)** iff the pooled \|t\| collapses below 2.0 OR the by-year table
  fails P1 or P2 → B4 joins the settled-null pile; do NOT paper-trade it. This is the de-risking win.

NO threshold relaxation, NO band sliding, NO post-hoc horizon promotion, NO max-over-years cherry-pick.

---

## EXACT CONSTRUCTION (reuse 18mo logic verbatim; only the panel depth changes)

1. **Tradeable entry/exit (anti-look-ahead):** entry = the **MOC 15:59-ET close** (the executable
   close, not a synthetic mark); exit = the **next session's open at ≥ 09:35 ET** (a tradeable open
   price, never the 09:30 print — the [[reference-quant-tradeable-entry-trap]] gap-fade look-ahead).
   Label = the 1d cross-sectional excess overnight return (within-day demeaned).
2. **Anti-bad-print hygiene (mandatory, from [[reference-overnight-data-traps]], applied SYMMETRICALLY):**
   - **$1 price floor** on BOTH legs (entry close and exit open) and on the entry row's X.
   - **Per-day symmetric winsorization** of the RAW overnight return at [0.5%, 99.5%] before the
     cross-sectional demean (symmetric → cannot inject directional bias).
   - **Label-distribution sanity check BEFORE scoring:** print `label_std` and max abs return; a sane
     1d overnight `label_std` is ≈0.027 (the 18mo value). An absurd std (e.g. 0.77 / 145× max) is the
     sub-$1 bad-print tell and **aborts the run** before any IC is read. Deep history (2016-2018) has
     MORE penny/odd-lot tape risk than the 18mo window — this check is load-bearing here.
3. **Point-in-time universe (rebuilt yearly):** for each (symbol, date) assign the trailing-20d-ADV
   cross-sectional rank from the **deep raw bars only** (no look-ahead), via the #149 stage-1 reduction.
   - **B4 band = rank 2000-4000** (terminates at the deepest ranked name that year).
   - **60-day-stable cohort filter (the #149 surface on top of the band):** a symbol qualifies for B4 on
     a date only if it has **≥60 prior labelled days** AND its band membership is **stable** — it has
     been in the rank-2000-4000 band on a **majority of the trailing 60 days** (not a one-day boundary
     straddler). This is the #149 "membership stability" filter; it removes names churning across the
     band edge that would otherwise inflate turnover and fake the cohort. Ranks are **rebuilt per year**
     (point-in-time ADV recomputed within each calendar year's trailing window), so the cohort is the
     small-cap names that were *durably* rank-2000-3787 at that point in history, not survivorship-picked.
4. **Walk-forward:** harness purge `horizon_minutes=1440`, within-day shuffle canary, NW lag=3,
   `cadence_min=390` → `periods_per_year=252`, `n_folds` as in the 18mo run. No leakage across the
   overnight boundary.
5. **Realistic cost:** per-band one-way overnight cost = **median Corwin–Schultz (2012) high-low
   half-spread (bps) + 1.0 bp** pad, recomputed point-in-time on the deep panel. (Caveat carried from
   the adjudication: CS under-states next-open auction spreads on small-caps; the deep gross-breakeven
   margin over CS-cost is necessary-not-sufficient and the REAL measured cost still gates capital.)

---

## READINESS GATE — when this RUNS (do NOT fire on the alphabetical partial fill)

The deep-bars panel is filling **alphabetically**. Running B4 on a partial alphabetical slice would test
an A-* biased sub-cross-section, NOT the rank-2000-3787 cohort — a garbage result either way. The test
fires ONLY when the deep panel has **representative coverage of the B4 small-cap cohort**.

**Concrete readiness check (run BEFORE the experiment; pure read, no scoring):**

1. **Cohort coverage ≥ 90%.** Take the 18mo B4 membership (the rank-2000-3787 symbols from the #149
   `/api/liquidity-bands/members/2000-4000` snapshot, ~1787-2000 names). Query the deep raw-bar
   coverage (#144 `/api/raw-coverage` pattern / `services/dashboard/raw_coverage.py`) for each. Require
   **≥ 90% of the B4 cohort symbols have deep bars spanning ≥ 8 of the ~10 years** (2016-2025). Rationale:
   the by-year persistence test (leg P) needs each year populated for the same cohort; partial-year or
   partial-cohort coverage cannot support a 7/10 by-year count.
2. **Not-alphabetical-biased.** Confirm the fill is no longer a contiguous alphabetical prefix: the
   B4 cohort's covered symbols must span the **full A-Z range** (e.g. ≥1 covered B4 name in each of
   ≥20 of the 26 leading-letter buckets), OR simpler — the backfill manifest reports **full-universe
   completion** (`7631/7631`, the cleanest trigger). The full-universe-done signal is the canonical
   green light; the 90%-cohort check is the earlier-fire fallback if cohort coverage saturates before
   the long tail.
3. **Deep-panel label-std sanity passes** on a smoke slice (leg-2 hygiene above) — abort if the deep
   tape's penny-print rate breaks the $1-floor + winsor guard.

When (1) AND (2) AND (3) hold → the test is ARMED to fire as a single faithful run with the frozen gate
above. Until then: **HOLD** (this is the TOM discipline — never run the resolver on biased partial data).

*Readiness state at stamp:* deep-bars ≈936-1618/7631 (alphabetical prefix) → **NOT READY**. Re-check
when the backfill manifest approaches full-universe (~over the 2026-06-20/21 weekend per Lead estimate).

---

## SECONDARY THREAD (note only, lower priority — pre-registered briefly as the next thread)

After B4-deep resolves, the next deep-panel thread is a **daily cross-sectional FACTOR baseline** on the
deep *liquid* panel (the well-powered, scalable, genuinely-different-from-intraday lead Lead flagged):
classic **12-1 momentum**, **short-term (1-5d) reversal**, and **low-volatility** factors, cross-sectional
on the top-1500-by-ADV liquid universe over ~10yr. Same disciplines (pre-register the gate first;
tradeable-entry; net-of-cost; by-year persistence; shuffle canary). This is a lower-novelty default and
is **NOT** part of the B4 gate — separate pre-registration when B4-deep is settled. Noted here only to
queue the thread; do not co-mingle its criteria with B4's.

---

## STATUS

**ARMED, not fired.** Pure pre-registration: frozen B4 gate (the exact 18mo five legs, no relaxation)
+ a TOM-style by-year persistence leg (≥7/10 positive full years, no significant sign-flip) + an explicit
readiness gate (≥90% B4-cohort deep coverage + non-alphabetical-biased / full-universe-done + deep label-std
sanity). NO data run, NO quantlib edit, NO live-tree change, NO fingerprint, NO deploy. Scratch in
`experiments/2026-06-19-laneC-scope-horizon/` (untracked). Fires when the deep panel covers the B4 cohort.
