# W14 — Activity-BURST / frequency-acceleration → multi-day drift (Ben's hypothesis, pre-registration)

**Registered:** 2026-06-16 BEFORE running. Ben's high-interest bet. DISTINCT from HF01-03: those tested quote
IMBALANCE / OFI (directional pressure) at sub-minute-to-30min horizons and died to turnover-compounded cost.
W14 tests trade/quote FREQUENCY ACCELERATION — a violent surge in the COUNT/intensity of trading activity —
as an ATTENTION / information-shock proxy, with the **2-DAY horizon as the PRIMARY bet**.

## Why 2-day is primary (friction-wall-aware design)
A frequency BURST is an attention/info shock; the interesting question is whether it drives MULTI-DAY drift
(the market digesting new information over days), not a microstructure wiggle. A 2-day hold is LOW-TURNOVER —
it sidesteps the turnover-compounded cost that KILLED HF01-03. So unlike the sub-minute imbalance work, a
2-day activity-burst signal is DESIGNED to clear the friction wall. We map 5min/30min too (the horizon-decay
curve) but the tradeable edge, if any, lives at 2-day (and longer).

## Hypothesis
A violent increase in a name's trade/quote frequency (a burst, defined precisely below) predicts a positive
forward return at the 2-DAY horizon (and we measure 5min/30min/1d/2d/5d to map decay) — strongly enough that
a LIQUID-tier, low-turnover, 2-day-hold portfolio clears net-of-cost OOS.

## Defining "violent burst" (precise, pre-committed)
Per (symbol, minute), from raw trades/quotes (63d) + the existing microstructure features:
- `freq_z` = z-score of the minute's trade COUNT vs the trailing-N-minute (e.g. 30-min) rolling
  mean/std of that name's per-minute trade count. A BURST = freq_z > k (test k ∈ {2, 3, 4}).
- `quote_accel` = d(quote-count)/dt over the trailing 5 min (acceleration of quoting intensity).
- `burst_intensity` = the `microstructure_burst` group's max-trades/sec (peak intensity) z-scored; and CV of
  inter-trade gaps (burstiness) from that group / `tick_runlength`.
- A "violent burst event" = a minute where freq_z > k AND burst_intensity elevated. Aggregate to a daily
  event (a name had a burst on day D) for the multi-day test.

## The PRIMARY CONFOUND (Ben flagged — control it)
Is the burst just reacting to an ALREADY-PUBLIC catalyst (earnings 8-K, news)? If so the multi-day drift is
the KNOWN PEAD / news-drift, not a pure activity signal. Control: cross-reference burst-day events against
the `filings` table (an 8-K / item-2.02 within ±1 day of the burst). SPLIT the cohort:
- **burst WITH an identifiable filing catalyst** (≈ PEAD/news — the known effect), vs
- **burst with NO identifiable catalyst** (the pure attention/activity signal — the novel, interesting one).
Report BOTH. The headline claim is the NO-CATALYST burst drift (if the only drift is catalyst-driven, W14 is
just a re-labelled PEAD/news effect, not a new signal).

## Universe + data + horizons
- LIQUID-tier PRIMARY (top ~300 by dollar-volume). Raw trades/quotes 63d for the burst definition; daily bars
  (now 378d on the liquid set) for the 1d/2d/5d forward returns. Intraday minute returns (5/30min) from bars.
- Forward returns: 5min, 30min (microstructure decay), 1d, **2d (PRIMARY)**, 5d. Entry = next tradeable bar
  after the burst (≥09:35 / the next session open for multi-day; UTC-correct, RESEARCH_PITFALLS #1). Sign: the
  prior is burst → positive drift (attention), but TEST the sign — a burst could also mark a top (reversal);
  let the data decide and sign the cohort honestly.

## Test design
- Cross-sectional, LIQUID-tier: rank/flag burst names each day; cohort forward drift vs same-day non-burst
  controls; OR a long-burst (signed by the predicted direction) portfolio, 2-day hold (low turnover).
- GATES: shuffle-canary; per-symbol demean; walk-forward OOS; per-trade bootstrap on NON-overlapping 2-day
  round-trips (CI excludes zero above); cost @ measured liquid spread + 2× (trivial at 2-day turnover — the
  friction-wall point). DECISIVE: LIQUID-tier OOS 2-day burst-drift net-of-cost, per-trade bootstrap CI > 0,
  AND the NO-CATALYST subset carries it (not just the PEAD/news subset).

## Expected / confidence
- Confidence the LIQUID 2-day NO-CATALYST activity-burst drift clears net-of-cost OOS with bootstrap CI > 0:
  **~30%.** Friction-favorable (2-day = low turnover, the HF-killer removed) and a real documented family
  (attention/volume-shock drift — Gervais-Kaniel-Mingelgrin "high-volume return premium"; Barber-Odean
  attention). Risks: (a) the drift may be ENTIRELY the catalyst (PEAD/news) confound — then it's not new; (b)
  attention effects are often small-cap (the liquid gate may kill it); (c) 63d of trades limits the burst-
  event count. Pre-commit the prior.
- KEEP-AS-LEAD: LIQUID 2-day NO-CATALYST burst drift OOS net positive, bootstrap CI > 0, demean+canary
  survived → an activity-burst paper container + certify deeper. AMBIGUOUS: only the catalyst subset works
  (= re-labelled PEAD), or liquid-marginal. KILL: no 2-day drift beyond canary OR net ≤ 0 OR the no-catalyst
  subset is dead (the signal is just news-reaction).

## Friction-wall scorecard
[attention/info-shock ✓ not microstructure pressure] [2-day LOW-turnover ✓✓ — the explicit fix for what
killed HF01-03] [liquid-gated ✓ PRIMARY] [the catalyst-control separates a NEW signal from re-labelled PEAD]
— a genuinely distinct, friction-wall-aware bet. The horizon-decay curve (5min→2d→5d) is a guaranteed
deliverable regardless of the verdict.
