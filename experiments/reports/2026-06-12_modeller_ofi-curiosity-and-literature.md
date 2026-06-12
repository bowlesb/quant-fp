# OFI: curiosity read + literature survey (pilot-prior shaping)

**Agent:** modeller (Research Lead) · **Date:** 2026-06-12 · **Status:** COMPLETE (curiosity + survey).
**NOT a verdict** — shapes the pre-registered OFI marginal-IC battery, which is gated on ≥10 sessions.

## 1. Hypothesis (pre-registered, pilot)
HEADLINE METRIC (Manager-set): OFI's MARGINAL within-ts rank-IC OVER ret_5m at 30m — does directly-
measured order flow beat the 5-min-return proxy it mechanistically explains? Configs A=price(ret_5m+pos),
B=price+OFI, C=OFI-only; PRIMARY = IC(B)−IC(A). Pre-registered PRIMARY confidence ~45% (later lowered).

## 2. Exploration
(a) CURIOSITY READ (⚠️ NOT a verdict, 50 names / 3 days): signed-volume imbalance (buy−sell)/(buy+sell)
vs forward-5m return, per-minute cross-sections, 44,510 obs.
(b) LITERATURE SURVEY (new protocol): arXiv q-fin / JFE / practitioner on OFI signal horizon, decay, cost.

## 3. Results
CURIOSITY: Pearson −0.032, Spearman −0.050 (NEGATIVE) = faint very-short-horizon MEAN-REVERSION. Right
sign for a microstructure effect, non-trivial magnitude, but 3 days = far too thin to trust; gates nothing.
LITERATURE (cited):
- Cont-Kukanov-Stoikov 2014 (JFE) [arxiv 1011.6402]: OFI→price LINEAR, slope INVERSELY ∝ market DEPTH ⇒
  OFI impact LARGER in thin/illiquid names (same signal-vs-cost tension as task #5).
- Cont-Cucuringu-Zhang 2023 (Quant Finance) [arxiv 2112.13213]: PREDICTIVE OFI content lives at <3 MIN,
  decays rapidly; contemporaneous fit strong but not tradeable.
- Markwick 2022 / Chordia-Subrahmanyam (JFE 2004): "looks profitable gross, costs DESTROY it" — OFI is a
  max-turnover signal; only institutions with minimal cost + good fills profit. = our task #5 cost wall.

## 4. Verdict / interpretation
The literature does NOT kill OFI but SHARPENS the bet. PILOT-PRIOR UPDATES (honest):
- PRIMARY confidence 45% → 35% (OFI's native horizon is <3min, so at 30m it may be the same decayed
  info ret_5m already captures).
- ADD a 5m/15m OFI cut at OFI's NATIVE horizon — if OFI only works faster than our 30m cadence, that's a
  harder production story (faster cadence = more turnover = the breakeven bar from task #5's curve rises).
- Confidence that net-of-cost stays below realistic cost even if marginal IC>0: ~70% → ~80%.
- Fill-probability (exec, Monday) is DOUBLY decisive — the literature names it as THE separator.

## 5. Next steps + declined
- ofi_marginal_battery.py: I own it, fires on the ≥10-full-session trigger (build now, run when data lands).
- Bake into pre-registration: the 5m/15m native-horizon cut; what "works at 5m not 30m" means operationally.
- DECLINED: trusting the curiosity −0.05 as signal (3 days); any OFI edge claim before NW t>3 at depth +
  net-of-measured-cost positive on a tradeable subset + survivorship-clean.
