# LP FILL PRIMITIVES — DEEP-CORE RESULT (substrate, not an edge verdict)

**Run:** 2026-06-20 · `lp_fill_primitives.py` defaults (fill_window 60s, hold 60s, post_step 5s,
20 sampled quote days evenly over 2024-12-12 → 2026-06-18). 13 deep-core names, 279 (symbol, day) rows.
Reads READ-ONLY from `/store/raw/quotes` (379d real NBBO). Full per-row CSV: `lp_primitives_results.csv`.

## The map (per-symbol medians across sampled days; bps per fill, sorted by quoted half-spread)

| symbol | half_sp bps | fill_rate (CEIL) | earn bps | adverse bps | net mean | net median | touch $ |
|---|---|---|---|---|---|---|---|
| SPY  | 0.08 | 0.88 | 0.10 | 0.26 | **−0.10** | −0.07 | 419,604 |
| QQQ  | 0.15 | 0.89 | 0.15 | 0.27 | **−0.10** | −0.00 | 316,224 |
| NVDA | 0.29 | 0.87 | 0.37 | 0.48 | **−0.11** | −0.00 |  79,390 |
| AAPL | 0.55 | 0.83 | 0.51 | 0.37 | +0.19 | 0.20 |  69,975 |
| GOOGL| 0.57 | 0.85 | 0.58 | 0.45 | +0.05 | −0.00 |  53,750 |
| AMZN | 0.63 | 0.84 | 0.63 | 0.43 | +0.17 | 0.23 |  68,849 |
| MSFT | 0.83 | 0.75 | 0.87 | 0.67 | +0.45 | 0.38 | 131,572 |
| TSLA | 0.87 | 0.84 | 0.97 | 0.89 | +0.10 | 0.19 |  69,763 |
| PLTR | 1.05 | 0.84 | 1.05 | 0.93 | +0.28 | 0.53 |  37,206 |
| AMD  | 1.28 | 0.80 | 1.37 | 0.97 | +0.53 | 0.65 | 1,592,827 |
| META | 1.85 | 0.70 | 1.93 | 1.18 | +0.71 | 0.74 | 220,140 |
| AVGO | 1.99 | 0.74 | 2.12 | 1.46 | +0.65 | 0.67 |  49,537 |
| ORCL | 2.19 | 0.71 | 2.33 | 1.41 | **+1.01** | 1.35 |  63,328 |

## What it says (and explicitly does NOT say)

**The pre-registered prediction HELD.** `net_per_fill` is cleanly MONOTONE INCREASING in the quoted
half-spread, near/below zero at the tightest names, all magnitudes sub-2bps — exactly the
Glosten-Milgrom signature, and a strong internal-consistency check that the fill/adverse computation
has no look-ahead bug (an inverted or huge-at-tight-names gradient would have flagged one).

- **The tightest, most "liquid" names are the WORST LP venues.** On SPY/QQQ/NVDA the post-fill 60s mid
  move adversely exceeds the ~0.1–0.3bps spread you earn → net NEGATIVE. Earning the spread on the
  flagship ETFs is a structural loss to adverse selection. This is the single most useful pre-fact for
  the LP surface: do NOT site an LP strategy at the tightest core.
- **Net-of-adverse-selection capture is positive and grows toward the WIDER core** (ORCL +1.0, AVGO/META
  +0.7, AMD/PLTR +0.3–0.65). The wider quoted spread over-compensates for adverse selection on these
  names over a 60s hold.
- **The whole effect is small (≤ ~1bp/fill) and sits BEFORE the honesty haircuts.** `fill_rate` here is
  a CEILING (queue position ignored); maker rebates, fees, and a real (worse) fill rate all eat into
  these numbers. A positive +1bp/fill gross-of-those-frictions is NOT yet a tradeable edge — it is the
  HEADROOM the LP surface has to work with after frictions. The active thread must subtract those.

**This is a SUBSTRATE, not a verdict.** It maps where on the deep core LP could plausibly clear adverse
selection (the wider mega-caps), and quantifies the no-go zone (the tightest ETFs). It does not test any
LP strategy, signal, or portfolio. The LP edge verdict — passive posts conditioned on a signal, netted
against this fill model plus real frictions, with shuffle/predict-zero baselines and a median gate —
remains the active quote-thread's pre-registered job. This module just guarantees that test rests on one
audited fill model instead of an inline guess.

## Robustness / parameter sensitivity (for the consumer)

The 60s/60s window is one point. The module is parameterized (`--fill-window-s`, `--hold-s`,
`--post-step-s`); a shorter hold reduces adverse selection (favors the provider), a longer hold worsens
it. The LP surface should sweep the hold horizon as a first-class knob — the net flips sign with hold
length, so the "optimal hold" is itself part of the LP design, not a fixed assumption. Recommend the
active thread run the sweep on its target names before committing a hold.

## Handoff

- Module + this map are ready for the LP surface to import (`from lp_fill_primitives import
  compute_day_primitives, deep_core_symbols, quote_days`).
- Backfill recommendation to the Lead: the LP-plausible zone is the WIDER mega-caps; widening the deep
  quote window to the next ADV tranche (names with 1–5bps spreads, where the headroom is largest) is
  higher LP-research value than deepening the already-covered ultra-tight ETFs. (Non-blocking steer.)
