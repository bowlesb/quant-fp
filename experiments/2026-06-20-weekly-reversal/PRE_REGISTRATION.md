# PRE-REGISTRATION — #2: WEEKLY short-term REVERSAL, survivorship-gated (the turnover/horizon attack)

**Date:** 2026-06-20  **Author:** Modeller  **Status:** GATE-READ REQUESTED (no numbers produced yet)
**Supersedes** the #205 design (experiments/2026-06-19-multiday-horizon/prereg.md) — same thesis, upgraded to
the Lead's standard: survivorship as a hard GATE with a CALIBRATED haircut, the now-accurate Stage-1 cost
amortized over the weekly hold, and a fully-locked anti-fooling spine.

**Thesis.** Every prior null (intraday/overnight, ~30-min, high turnover) died on NET-OF-COST. A WEEKLY hold
amortizes one round-trip cost over ~5 trading days, so a per-period-weaker signal can clear net. Weekly
short-term REVERSAL (buy last week's cross-sectional losers, short last week's winners) is the most-documented
low-turnover anomaly and the natural test of the cost-amortization hypothesis.

---

## 0. ⚠️ THE MAKE-OR-BREAK: SURVIVORSHIP (calibrated, gated — not a footnote)

**Verified survivorship facts (measured now, not assumed):**
- The deep `fp_store_real` bar panel (7,703 syms, 2016→2026) is **PERFECTLY survivorship-biased: 0 of 600
  sampled symbols stop printing before 2026-06-17** — every name prints right up to today. ~99% have full
  history back to 2016; ~1.2% are later listings (IPOs). **There are ZERO in-sample delistings to observe.**
- `universe_membership` (the survivorship-aware table) only starts 2026-06-15, so a historical universe MUST
  be reconstructed from bars = survivors-only by construction.

**Why this is the centerpiece for THIS strategy specifically.** Weekly reversal BUYS recent losers. The names
that fell hardest and then KEPT falling — to delisting/zero — are exactly the ones absent from a survivors-only
panel. So "buy the losers, they bounce" is flattered BY CONSTRUCTION: the panel's bottom-decile is censored to
losers that survived (and thus disproportionately bounced). A reversal "win" here is suspect until the censored
losers are accounted for. **This is the bias that has killed loser-buying strategies for decades; it is the
gate, not a caveat.**

**Because in-sample delistings = 0, the haircut CANNOT be an empirical in-panel rate — it must be EXTERNALLY
CALIBRATED.** Pre-committed model (the survivorship GATE):

- **Delisting base rate (externally calibrated, literature-grounded):** US-listed common stocks delist at
  ~**5-8%/year** unconditionally (CRSP-documented; ~half are M&A/neutral, ~half are
  performance/bankruptcy/exchange-rule = adverse). Conditional on being in the **bottom-return decile** (the
  loser leg we BUY), the adverse-delist hazard is materially elevated — pre-commit a conservative
  **bottom-decile adverse-delist rate of 1.0%/MONTH ≈ 0.23%/WEEK** for the loser leg (this is the rate the
  haircut charges; it is FIXED here, not tuned to any result). The winner/short leg uses the unconditional
  adverse rate (~0.05%/week) — short legs are HELPED by delisting, so we conservatively assume zero benefit
  there (charge the loser leg, credit the short leg nothing).
- **Loss-given-delist (LGD):** an adverse delist realizes a terminal return drawn from the CRSP delisting-return
  literature — pre-commit **LGD = −55%** as the base case. This is the canonical Shumway (1997, *J. Finance*
  "The Delisting Bias in CRSP Data"; 1999 Nasdaq follow-up) correction: the standard value applied to missing
  performance/bankruptcy delisting returns, which CRSP systematically omits and which are large and negative.
  A **−100% (total-loss) stress** is reported alongside as the worst case. The −55% is thus literature-grounded,
  not invented.
- **How it is charged (the GATE):** each weekly rebalance, the loser-leg basket return is debited
  `p_delist_week × LGD` (e.g. 0.23% × −55% ≈ **−13 bps/week** base, −23 bps/week at −100% LGD) — a per-week
  drag applied to the bought-losers leg BEFORE the pass bar is evaluated. The edge must clear net-of-cost
  **AND** net of this survivorship haircut. State the haircut magnitude in the result.

**The GATE (pre-committed pass bar, survivorship leg):** the weekly-reversal L/S net edge must remain positive
and significant AFTER the base haircut (−13 bps/week loser-leg drag), and its sign must SURVIVE the −100%-LGD
stress (it may shrink, but if it flips sign or loses significance under the stress, it is reported as a
survivorship artifact — the B4 outcome — NOT an edge). A win that only exists gross or only before the haircut
is NOT a pass.

> Honest framing: because the haircut is externally calibrated (not measured in-panel), it is itself an
> ESTIMATE. So the verdict is reported as a BAND: edge after [base haircut] and edge after [−100% stress]. If
> the edge is comfortably positive across the band → robust; if it lives only at the optimistic end → flagged
> as survivorship-fragile, not banked. The Lead scrutinizes the calibration; the numbers above are the
> pre-committed defaults and may be revised at gate-read BEFORE any run.

## 1. The hypothesis (single, pre-committed — no horizon/param fishing)
**H — weekly cross-sectional reversal.** Buy the bottom-decile, short the top-decile by `rev_1w` (trailing
5-trading-day return as of the Friday close), rebalanced weekly, held one week. ONE horizon (5d/5d), ONE
universe rule (§3), ONE feature. Reversal predicts a POSITIVE rank-IC of `−rev_1w` vs forward weekly return.
(The secondary low-vol H2 from #205 is DROPPED from this pre-reg to keep the family at N=1 — minimal fishing
surface; it can be a separate pre-reg.)

## 2. Tradeable timing (no look-ahead — the platform's #1 false-edge source)
- Signal as-of **Friday RTH close** (`rev_1w` = close[Fri]/close[Fri−5d] − 1).
- Entry at the **following Monday (next session) tradeable open ≥ 09:35 ET** — NEVER the Friday close
  (close-to-close look-ahead = the gap-fade trap). Forward weekly return booked from that tradeable entry to
  the next Friday's tradeable price.
- $1 floor on BOTH legs; per-week symmetric winsorization of returns (the overnight/multi-day bad-print trap).

## 3. POINT-IN-TIME universe (fixes the #205 stub's look-ahead)
**Universe each week = top-N by TRAILING-20d ADV computed AS-OF that week's Friday**, reconstructed from bars,
N = 1000 (liquid large/mid-cap, where abrupt delisting is rarest so residual survivorship is smallest). The
#205 build_weekly.py stub ranked ADV on a single mid-span day (look-ahead + survivorship); this pre-reg
REQUIRES per-week point-in-time ADV. A name is in week W's universe only if it prints in the trailing window
ending at W (no carrying a 2024 IPO back to 2018; no carrying a name into a week it didn't trade).

## 4. Cost — the now-accurate Stage-1 cost, amortized over the weekly hold (the whole point)
- Per-name round-trip cost = 2 × the Stage-1 measured realized half-spread at the Monday entry (reuse
  `quantlib.data.realized_cost.realized_half_spread_bps`), charged ONCE per weekly hold (not per day). This is
  the genuine test: does the weekly amortization let a real signal clear the ACCURATE cost the intraday hunts
  died on? Report net edge at the measured cost + the break-even cost in bps.
- NOTE on substrate: Stage-1 realized cost needs the quote tape, which is broad only 2026-03-31+. For the deep
  multi-year run (2016→2026) the quote tape is absent, so the deep run uses a **conservative bar-derived cost
  proxy** (a documented bps floor) and the RECENT sub-window (2026-04+) is re-run with the true Stage-1 cost
  as the cost-accuracy check. Both are reported; the deep run's cost is explicitly an estimate.

## 5. Anti-fooling spine (all pre-committed)
- **Own-vol / SIZE control (CRITICAL — the 10/13-survivor killer):** partial out trailing realized vol +
  log-ADV from BOTH `−rev_1w` and the forward return; reversal must retain IC after (else it is just "small
  illiquid names bounce" = a size/illiquidity tilt, not a tradeable reversal). Report the collapse ratio.
- **Walk-forward, purged** by the 1-week horizon (no label leak); OOS by period (fit early years / confirm
  late) + per-year IC sign-consistency.
- **Shuffle baseline** (permute the forward label within each weekly cross-section, ≥200 iters) + **predict-
  zero**. The real weekly-IC series must dominate shuffle.
- **Per-WEEK NW-t significance** of the weekly IC series AND of the L/S basket weekly return — |t| ≥ 2.0 and
  NOT driven by one outlier week (report the per-week distribution + max-week share, as the cost-regrade did).
- **Disjoint-window replication** required for any pass (split the span into two non-overlapping multi-year
  halves; pass must hold sign + significance + post-haircut on both).
- **BY-FDR q=0.10** across all (control-variant) cells; N pre-committed (= the cells actually tested; with one
  horizon/one feature the family is small — list it before running).

## 6. The full pass bar (ALL must hold — a single leg failing = NULL)
1. Reversal rank-IC > 0, dominates shuffle, per-week NW-t ≥ 2 (not one outlier week);
2. survives the own-vol/size control (retains material IC);
3. L/S net edge positive at the measured/proxy cost amortized over the weekly hold (+ break-even reported);
4. **net edge survives the survivorship base haircut (−13 bps/week loser leg) AND keeps its sign under the
   −100%-LGD stress** (§0 — the centerpiece gate);
5. replicates on a disjoint window;
6. survives BY-FDR.

## 7. PRE-COMMITTED NULL BRANCH (inclusion-liberal disposition, per Ben)
If it dies at any leg: report cleanly with the failing leg + the break-even cost + the haircut magnitude.
**Disposition (Ben's standing principle):** a $-null = the current MODEL doesn't TRADE weekly-reversal yet on
this substrate — NOT that `rev_1w` / the weekly features are worthless. The reversal/vol features stay
INCLUDABLE/retained (cheap, parity-true); inclusion is decoupled from $-value; future data (a delisting-
inclusive universe, deeper quotes) or interactions may revive it. Specifically: if it dies ONLY under the
survivorship haircut, that is the decisive, honest quantification of the bias (a clean result), and the
natural follow-up is ACQUIRING a delisting-inclusive universe (CRSP-style) — flagged to the Lead, not chased
on the biased panel.

## 8. What this is NOT
- NOT a momentum/winner-buying bet (survivorship would flatter it in the DANGEROUS direction).
- NOT a multi-horizon/param sweep (ONE 5d/5d horizon, ONE feature — minimal fishing surface).
- NOT a gross or pre-haircut "win" (§6 requires post-cost AND post-survivorship-haircut).
- NOT run yet — gate-read first. The survivorship calibration (base rate, LGD, the −13bps/week charge) is the
  part to scrutinize; send revisions BEFORE any number. Turn-key build/screen exist (#205) and will be
  upgraded to the point-in-time universe + the calibrated haircut gate + Stage-1 cost on approval.
