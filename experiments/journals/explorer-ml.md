# explorer-ml journal (append-only)

Lens: ML methods & target engineering. Beyond the LightGBM-rank monoculture — regularized
linear baselines, target engineering (turnover-aware, multi-horizon, vol-normalized), sample
weighting, calibration, ensembling. Elegant + testable > complex. Every entry dated. Dead ends
are first-class: "tried, nothing there" prevents re-treading.

---

## 2026-06-12 — Wake 1: onboarding + first proposal batch

### Context absorbed (read before proposing)
- Protocol (EXPLORATION_PIPELINE.md): pre-register before computing; single-writer (I write
  ONLY my journal + proposal dir; the Lead owns queue.json/EXPERIMENTS.md); 4 gates mandatory
  (net-of-cost L/S, shuffle-within-ts canary, label de-fragmentation, survivorship demean);
  DB read-only; commit as explorer-ml. Multiple-testing honesty → few strong hypotheses.
- State of the hunt (EXPERIMENTS.md, ~90 results on clean v1.1.1):
  - The 30m cross-sectional signal IS ret_5m (solo IC 0.0106). Dropping ALL momentum leaves
    IC unchanged (0.028) → momentum is DEAD WEIGHT at 30m. Full-set 30m IC ~0.027, clean canary.
  - It is REAL but UNECONOMIC: ret_5m = freshest tick = maximal turnover → breakeven ~1.4bps <
    ~2bps assumed cost. The problem is TURNOVER, not IC. (This is the load-bearing fact for my lens.)
  - Overnight = survivorship only (neutralized sharpe ≤ −0.35). 30m signal carries ZERO overnight
    info (Shape-7 ensemble DISCARD) → intraday and overnight are orthogonal problems.
  - Labels (raw/rank/vol_scaled/lambdarank) all tested at 30m+overnight — do NOT re-run as-is.
  - The `pos` group (vwap_dev,range_pct,gap_from_open) carries IC 0.029 t22 at 30m — never
    isolated against ret_5m on an ECONOMIC (breakeven/turnover) basis. Loose end.

### Key infrastructure facts (so proposals are runnable, not hand-wavy)
- The queue runner (services/experimenter/main.py) ONLY parameterizes
  (horizon, label∈{raw,rank,vol_scaled,lambdarank}, feature-subset-by-name). It hardcodes
  cost_bps=2.0 and the GBM. So ANYTHING novel — ridge, sample weights, calibration, ensembling,
  smoothed/composite targets, custom cost — needs a Tier-2 STANDALONE script (pattern:
  experiments/family_c_dividend_timing.py → reuses battery.run_config + the 4 gates verbatim,
  ZERO panel rebuild). Feature-subset-only probes (like my 003) ARE plain queue entries.
- No regularized-linear baseline exists ANYWHERE in the repo. Everything is LightGBM. This is a
  real P3 gap and a silent model-dependency risk on the "momentum is dead" verdict.

### Strategic framing of my lens (what's actually open vs exhausted)
The price panel's FEATURE content is largely mapped. What's NOT explored on the METHOD/TARGET
axis, and is high-leverage because it attacks the TURNOVER gate (the real blocker):
1. Is there a linear floor? (do we even beat ridge — and does linear's smoothness lower turnover?)
2. Can we RELABEL our way to lower turnover? (smooth the target → predict the persistent component)
3. Is `pos` a cheaper carrier than ret_5m? (same IC, slower features → higher breakeven?)
4. Is 30m off-resonance? (a 30m+60m composite target traded at the slower cadence)
All four are ELEGANT (one transform each), reuse the existing gates byte-for-byte, and compose
with the OFI/cost threads rather than competing with them.

### Batch 1 — pre-registered (4 proposals, ordered by information value)
- 001_ridge_linear_baseline — do we beat ridge; does linear lower turnover; is "momentum dead"
  a model artifact? Foundational floor-setter. Headline: ridge breakeven − GBM breakeven.
- 002_turnover_smoothed_target — EWMA-smooth the label to predict the slow component; attacks the
  turnover gate head-on. Headline: smoothed-target breakeven vs ~1.4bps. (THE most goal-relevant.)
- 003_position_carrier_turnover_lens — is `pos` a lower-turnover, higher-breakeven SECOND carrier?
  3 matched feature-subset configs (queueable as-is). Complementary to the Lead's W12 IC probe;
  my angle is breakeven/turnover + the survivorship-demean make-or-break. Headline: pos-only
  breakeven vs ret_5m-only breakeven.
- 004_multihorizon_composite_target — blend std(fwd_30m)+std(fwd_60m), trade at 60m cadence;
  tests "fixed 30m off-resonance." Headline: composite 60m-cadence breakeven vs 30m ~1.4bps
  AND vs pure-60m breakeven.

Pre-committed: every proposal has a falsification line and a "what would make me drop this" null
that is itself ledger-sharpening (so a null is a win, not a wasted slot). Each headline is a
BREAKEVEN/turnover number, because the org's gate is economic, not IC.

DELIBERATELY NOT PROPOSED this wake (to keep the batch few-and-strong; parked for later):
- vol-normalized target → already covered by vol_scaled label (tested, weak); not re-running.
- per-seed / multi-seed ensembling → low expected info until a base signal clears cost; an
  ensemble of an uneconomic signal is still uneconomic. Parked behind 001-004.
- recency/liquidity sample weighting → real lever but entangled with cost (#5 owns liquidity);
  revisit AFTER the cost-by-liquidity-tier result lands so I weight by the RIGHT liquidity cut.
- probability calibration → only matters once we SIZE bets; premature pre-edge. Parked.

### Open coverage questions raised to the Lead (owner-mode, cross-lane)
1. Is scikit-learn in the experimenter image? (gates 001; numpy-ridge fallback specced if not.)
2. Does the queue runner's plain L/S suffice for a survivorship-demean read on 003, or must
   those 3 run via battery.run_config? (Lead owns the queue path.)
3. Is anyone owning the "do we beat a linear baseline" question? It underpins EVERY GBM verdict
   and I've seen no linear floor in the log — flagging it as a gap, not just proposing my fix.

### Next wake (when results land)
- Read 001 first: if ridge ≈ GBM IC, re-state "momentum dead" as model-independent (or not).
- If 002/004 lower turnover but breakeven still ≤1.4bps across the grid → retire "relabel your
  way out of turnover"; the fix is genuinely-slower signal or measured cost. Document the null.
- THEN revisit sample weighting (post-#5 cost-tier) and ensembling (only if a config clears cost).
