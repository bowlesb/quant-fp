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

---

## 2026-06-12 — Wake 1 cont: Lead dispositions + 001 ridge BUILT & smoke-validated

Lead enqueued ML-003 (my 3 exact pos-vs-ret5m configs, after the W12 block). Build order set:
001 ridge FIRST (load-bearing), then 002, then 004. Lead owns the per-symbol-demean follow-up
on 003 (queue runner does L/S but not demean).

### 001 built: experiments/ml_ridge_baseline.py
- sklearn is NOT in the experimenter image (checked) -> used CLOSED-FORM ridge
  (XtX+aI)^-1 Xt y, no new dep (as pre-committed). ElasticNet/L1 dropped (no closed form;
  the L2 path answers the floor+turnover question). Fold-local median-impute + standardize
  (leakage-safe). Alpha picked by inner-split on the FIRST train fold only. Labels: raw + rank.
- Mirrors the battery's 4 gates byte-for-byte (canary, net-of-cost L/S, survivorship demean),
  reusing battery.per_symbol_demean / filter_smoke / PRICE_ONLY_DROP and quantlib.backtest.
- CANARY FIX during build: canary now applies the SAME label transform to the within-ts-shuffled
  return (rank-of-shuffled for the rank config), so it's an apples-to-apples leakage arbiter.

### Smoke (120d) — harness validated, canary clean, directional read (NOT a verdict)
ridge/rank: IC 0.0179 t3.9, canary -0.0069 | ridge/raw: IC ~0 (degenerate, a=1.0).
SANITY CHECK that vindicates the canary: I ran the GBM battery on the SAME 120d window — its
canaries are -0.0092/-0.0042/-0.0127/-0.0027. So my ridge canary magnitude (~0.007-0.017) is the
SAME smoke-window noise floor the GBM exhibits on 120d, NOT a ridge leak. On full 600d both
collapse toward 0. Harness is clean.

DIRECTIONAL FINDING (full-depth needed to confirm, Lead's verdict):
- Ridge/rank IC 0.0179 vs GBM/rank 0.0235 on the SAME window -> ridge recovers ~76% of the GBM
  rank IC. A linear model captures MOST of the signal -> consistent with 001(a): the signal is
  largely LINEAR; GBM nonlinearity buys little here.
- Ridge/rank standardized coefs are dominated by vwap_dev (-0.0089), vol_30m, rel_ret_30m,
  mom_1d_rel, gap_from_open — i.e. the POSITION group (vwap_dev, gap_from_open) + a momentum-REL
  term carry weight. This is a DIFFERENT attribution from the GBM's "ret_5m is everything" gain
  story. EXACTLY the model-dependency 001 was built to surface: on the linear view, "momentum is
  dead / signal is purely ret_5m" looks model-dependent, not airtight. Ties straight into 003
  (is pos a real carrier?). FLAG for the Lead: do NOT treat full-depth ridge coefs as confirming
  the GBM gain story until checked — they may partly diverge.
NOTE on smoke economics: like the GBM, ridge is net-negative / breakeven <1.4bps on 120d (turnover
~2.2-2.6). The breakeven HEADLINE (does linear's smoothness lift breakeven vs GBM) needs full depth
— 120d is too short and too high-turnover to read the economic gate.

QA: ruff clean, black formatted, py_compile OK. mypy's only gripe is the psycopg stub (host-only,
hits every experiment script identically — not a real error). Committed; handed to Lead to enqueue
full-depth. NEXT: build 002 (turnover-smoothed target).

---

## 2026-06-12 — Wake 1 cont: 002 + 004 BUILT (smoke pending container)

### 002 built: experiments/ml_turnover_smoothed_target.py
Target = per-symbol FORWARD EWMA of the raw fwd_30m label over the next K IN-DAY cadence rows
(window truncated at the day boundary so an intraday target never averages across an overnight
gap). label="raw" path => transform_label is identity, so collect_oos trains the GBM on my
pre-smoothed array while the realized series stays raw y — reuses the battery GBM fold loop +
all 4 gates byte-for-byte. Canary shuffles RAW y (features-only leakage arbiter; the smoothed
target legitimately uses future returns, but the FEATURES must stay clean). Grid (k,hl) =
(2,1),(3,1),(3,2),(5,2) + a k=1 baseline that is provably == raw (weights=[1.0]). HEADLINE =
does any smoothed config lift breakeven above the raw ~1.4bps by cutting turnover faster than
gross falls.

### 004 built: experiments/ml_multihorizon_composite.py
Loads BOTH fwd_30m + fwd_60m panels, inner-joins on (symbol, ts) (drops rows missing either
horizon — never fabricates a label), target = within_ts_zscore(y30) + within_ts_zscore(y60)
(scale-fair blend). Trains GBM on the composite; grades IC vs BOTH raw horizons and runs the L/S
at BOTH 30m and 60m cadences; per-symbol-demean at each. Purges with the LONGER horizon (60m).
Reference runs pure_30m + pure_60m on the SAME joined panel for a fair comparison. Canary shuffles
raw y30. HEADLINE = composite breakeven at 60m cadence vs 30m-native ~1.4bps AND vs pure_60m.

Both lint clean (ruff+black), compile OK. Smoke validation pending — the experimenter container
runs one heavy job at a time, so I'm serializing: 002 smoke first (5 GBM configs x 2 walk-forwards
on ~1M rows = slow), then 004. Will commit each once its smoke proves the harness end-to-end +
canary clean, then hand the full-depth runs to the Lead to enqueue.

### 002 SMOKE RESULT (120d) — mechanism VALIDATED, harness clean
tag                IC      t      canary    breakeven  turn   surv_sh
raw_baseline_k1    0.0151  4.46   -0.00923  0.98       2.761  -4.92
smoothed_k2_hl1    0.0135  3.76   -0.00923  0.80       2.461  -4.68
smoothed_k3_hl1    0.0118  3.24   -0.00923  0.66       2.337  -4.82
smoothed_k3_hl2    0.0123  3.36   -0.00923  0.95       2.188  -4.21
smoothed_k5_hl2    0.0127  3.41   -0.00923  1.25       2.021  -2.52

READS: (1) canary IDENTICAL (-0.0092) across configs — correct, it shuffles raw y (features-only
arbiter), so smoothing the TARGET can't move it. Matches the GBM raw canary on the same window =>
harness clean. (2) k1 reproduces GBM raw EXACTLY (IC 0.0151) — the baseline is faithful. (3) The
MECHANISM works: smoothing LOWERS turnover monotonically (2.76->2.02) and IMPROVES survivorship
sharpe (-4.9->-2.5); IC falls gently (0.0151->0.0127, all t>3.2, all above canary). (4) Breakeven
is directional-positive at the most-smoothed end (k5_hl2 1.25 > baseline 0.98) — turnover fell
faster than gross — BUT 120d is too short/noisy for the economic verdict. Full depth (600d) decides
whether any config clears the ~1.4bps line. The IC<->turnover FRONTIER is exactly as proposed.
Committed (b73f794). 004 smoke launched next (unbuffered + results-file monitor to dodge the
stdout-buffering trap that hid 002's streaming output).

OPS NOTE: piping `docker compose exec` stdout to a file buffers until process exit, so a
line-grep monitor never fires mid-run. FIX for 004: run with `python -u` AND monitor the
in-container RESULTS FILE row-count (completion = N records) rather than stdout lines.

---

## 2026-06-12 — Wake 1 cont: Lead flagged ridge canary; DIAGNOSED = NOISE not bug + lit search

Lead HELD the 001 verdict: ridge smoke canary -0.0168 (raw) vs his expected ~0. He gave 3
diagnostic hypotheses. I ruled out ALL THREE with a focused diagnostic (/tmp, container, removed
after):

DIAGNOSTIC (120d, 3 seeds + alpha re-tune + fixed alpha):
  raw  seed=13 canary -0.01684 | seed=99 -0.00584 | seed=7 -0.00328 | pred_spread 6.7e-05
  rank seed=13 canary -0.00686 | seed=99 -0.00554 | seed=7 +0.00272 | pred_spread 3.2e-03
  RE-TUNED alpha on shuffled target -> SAME canary. FIXED alpha=10 -> SAME canary.
300d window (raw): seed=13 +0.00842 | seed=99 +0.00503 | seed=7 +0.01332  <- SIGN FLIPPED vs 120d

VERDICT: it's NOISE, not a structural bug. Evidence, point by point vs the Lead's hypotheses:
- H1 (canary scored vs wrong realized vector): RULED OUT. collect_oos_ridge returns realized=y
  (raw return) and per_timestamp_ic(canary_preds, canary_real=y, ...). Re-tune/fixed-alpha don't
  move it; the wiring is correct.
- H2 (degenerate/constant shuffle): RULED OUT. canary pred_spread within ts is NON-zero
  (raw 6.7e-05, rank 3.2e-03) — predictions vary, the shuffle isn't constant/sign-flipped.
- H3 (alpha coupling — canary inherits the real-tuned alpha): RULED OUT. Re-tuning alpha ON the
  shuffled target gives the SAME canary; fixed alpha=10 gives the SAME canary. Alpha is not it.
THE TELL: the canary SWINGS sign and magnitude with the seed (-0.0168/-0.0058/-0.0033 at 120d)
and FLIPS SIGN between window sizes (-ve at 120d, +ve at 300d). A structural leak would be stable
in sign+magnitude. This is sampling noise whose scale is set by #test-timestamps.

ROOT CAUSE: the canary estimator = mean over test-ts of per-ts Spearman IC, over only 5 folds.
On a 120-300d smoke the effective #independent timestamps is small (intraday rows are highly
autocorrelated within a day), so the noise floor is ~+/-0.01 — for BOTH ridge AND GBM (I'd
already calibrated: GBM 120d canaries -0.0027..-0.0127; my 004 GBM canary -0.0017). The seed=13
raw -0.0168 is the unlucky tail. IMPORTANT GENERAL FINDING: the canary noise floor (~0.01) is
COMPARABLE to the real IC (~0.02) at smoke depth, so IC/canary separation is marginal on a smoke
for EVERY model — the full ~600d panel is what makes the canary a trustworthy arbiter. This isn't
a ridge problem; it's a smoke-depth property the whole team should treat as a standing caveat.

ACTION: no code change needed (the harness is correct). Run 001 at FULL DEPTH; only then read
IC/canary. Reported the diagnosis to the Lead.

### LITERATURE (new binding protocol — my lens: cross-sectional equity ML + what replicates)
1. Gu, Kelly, Xiu (2020) "Empirical Asset Pricing via Machine Learning", RFS 33:2223. THE
   reference. Trees/NNs beat linear, BUT the gain is MODEST and traced to NONLINEAR INTERACTIONS;
   all methods agree on the same dominant signals (momentum, liquidity, volatility). At MONTHLY
   horizon. TAKEAWAY for 001: literature SUPPORTS "linear captures most of it" — my smoke ridge
   recovered ~76% of GBM rank IC at INTRADAY horizon, consistent. The remaining GBM lift is the
   interaction term GKX describe. TRANSLATION CAVEAT: GKX is monthly; intraday S/N + turnover are
   very different — published linear-vs-tree gap doesn't transfer 1:1, it's a hypothesis to test.
   https://academic.oup.com/rfs/article/33/5/2223/5758276
2. Transaction-cost-aware ML / signal smoothing (2024-26 arxiv: FR-LUX 2510.02986; band-turnover
   regularization; + Garleanu-Pedersen "Dynamic Trading with Predictable Returns and Transaction
   Costs", NBER w15205). STRONG support for 002: a documented result — "without smoothing net
   Sharpe -1.24 despite gross 2.27; a 21-day MA cuts turnover 82% and turns it profitable; gross
   Sharpe declines (signal decay) but turnover reduction more than compensates." This IS my 002
   mechanism (smooth the target -> lower turnover -> economic even as gross IC falls), and my smoke
   already showed the monotone turnover drop (2.76->2.02). Garleanu-Pedersen theory: weight
   SLOW-decaying predictors more relative to fast-alpha-decay ones. TRANSLATION: their smoothing
   is over DAYS (21d MA); ours is intraday cadence steps (K=2-5 over ~30-150 min) — same logic,
   our horizon means a much shorter smoothing window suffices. https://www.nber.org/system/files/working_papers/w15205/w15205.pdf
3. (empty/low-yield) "de-noising shrinkage noisy financial targets" — mostly covariance-matrix
   shrinkage (Ledoit-Wolf), not target-side. Logged as low-yield for the target-engineering angle;
   the ridge L2 IS the shrinkage answer on the FEATURE side (001). Not pursuing a separate probe.
LITERATURE INFORMS, never replaces gates: GKX's published linear-vs-tree result and the smoothing
Sharpe numbers are HYPOTHESES here, decided by our canary + net-of-cost gates at full depth.

---

## 2026-06-12 — Wake 1 WRAP: deliverables + open threads

DELIVERED this wake (all committed as explorer-ml):
- 4 pre-registered proposals (experiments/proposals/explorer-ml/001-004).
- 3 built + smoke-validated scripts: ml_ridge_baseline.py (001), ml_turnover_smoothed_target.py
  (002), ml_multihorizon_composite.py (004). All reuse the battery's 4 gates byte-for-byte.
- ridge-canary flag DIAGNOSED (smoke-depth noise, all 3 Lead hypotheses ruled out) — no code change.
- Literature logged (GKX 2020; cost-aware-ML + Garleanu-Pedersen) per new protocol.
- 3 reports filed (experiments/reports/2026-06-12_explorer-ml_*).
- ML003 (my 3 pos-vs-ret5m configs) enqueued by the Lead; 1/3 landed (ret5m_only breakeven 0.38).

SMOKE READS (directional, full-depth = verdict): 004 is the most promising (composite breakeven
60cad 1.48 dominates pure_30m 1.12 / pure_60m 0.74 + best survivorship). 002 mechanism validated
(monotone turnover drop, IC>canary). 001 ridge recovers ~76% of GBM rank IC (linear captures most)
but leans on the POSITION group, not ret_5m — a model-dependency flag on "momentum is dead."

OPEN THREADS (both will notify me):
1. Lead to run the 3 full-depth commands (001/002/004) — verdicts are his.
2. ML003 pos_only + ret5m_pos to land (monitor armed) → then write the ML003 report (the pos-vs-
   ret5m breakeven COMPARISON is the headline). EARLY: ret5m_only breakeven 0.38 < full-signal 1.4
   (ret5m = maximal turnover, as expected); pos_only breakeven is the number that decides 003.

NEXT-WAKE PROPOSAL CANDIDATES (parked, contingent on full-depth results):
- If 002/004 clear cost: a combined smoothed-composite target (one lever proven at a time first).
- If ridge IC gap vs GBM is impute-driven: drop-NaN-rows robustness cut.
- Post-#5 cost-tier: recency/liquidity SAMPLE WEIGHTING (weight by the right liquidity cut).
- Multi-seed ENSEMBLING only AFTER a base config clears cost (ensemble of uneconomic = uneconomic).
