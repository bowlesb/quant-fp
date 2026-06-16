# Hypothesis (PRE-REGISTERED — written before building data or running) — H2: OFI marginal lift over vwap_dev

**Author:** Modelling Agent · **Date:** 2026-06-15 · **Resource:** CPU-only (bounded cycle).

## Idea
The only proven intraday cross-sectional carrier is `vwap_dev` mean-reversion (negative, ~IC 0.028, model-
independent). The Lead's primary better-data bet is **order flow**. The load-bearing question for whether
order flow is worth platform investment:

> **Does signed order-flow imbalance (OFI) add cross-sectional forward-return IC ORTHOGONAL to vwap_dev at
> a 15–30 min horizon?** Per the W12 attribution correction, the baseline MUST be vwap_dev (the true
> carrier), NOT ret_5m (a weak proxy) — else OFI's lift is overstated. The headline is OFI's MARGINAL IC
> over a vwap_dev baseline, and whether it moves breakeven, not just IC.

## Data I will BUILD myself (Alpaca historical — out of the production store)
- **Universe:** ~300 liquid US equities (a fixed liquid set; survivorship caveat noted — these are names
  alive today, so this is NOT a survivorship-free test, only a marginal-IC orthogonality probe).
- **Days:** ~8 recent trading days (≈ 2026-06-02 … 2026-06-12), RTH only (13:30–20:00 UTC).
- **Bars (1m, SIP):** for vwap_dev (deviation of close from the session cumulative VWAP) and forward returns.
- **Trades (SIP):** to build OFI myself — tick-rule signed volume per minute, then `signed_vol_z` and
  rolling OFI windows (5/15/30m). MUST exclude minutes ≥ 15:50 ET (MOC distortion — Modeller spec in
  EXPERIMENTS.md). Stored under `experiments/data/` (scratch), never the live `/store`.
- **Forward return:** booked from a TRADEABLE entry — the return is `close(t+H)/close(t+1) − 1` style with
  entry no earlier than the bar AFTER signal time (no 09:30-print / same-bar look-ahead). Documented exactly
  in method.md. Cross-sectionally demeaned within each minute.

## Method (3-arm marginal-IC battery, gates)
Within each RTH minute cross-section, demean the forward return, then measure within-minute rank-IC for:
1. **baseline** = vwap_dev (+ optionally ret_5m, gap) — the true carrier set.
2. **+OFI** = baseline + signed_vol_z / OFI windows.
3. **OFI-only** = OFI features alone.
Pool IC across minutes×days (mean IC + rough t via per-(day) or per-(minute) std). Gates: (a) within-minute
rank-IC, (b) **shuffle canary** (permute forward return within minute — multi-seed), (c) a crude net-of-cost
read (turnover × a representative spread) — honest but not the full measured-cost model. Headline = does
+OFI's IC EXCEED baseline's by more than the canary band, AND is OFI-only itself non-trivial?

## EXPECTED result (committed BEFORE building/running — the falsifier)
- **Primary (confidence ~40%):** OFI carries SOME standalone within-minute IC at 15m (|IC| ~0.01–0.03,
  same-sign-as-continuation, i.e. POSITIVE signed-flow → positive forward return) with a clean canary.
  I lean toward order flow having a real short-horizon signal (Cont-Kukanov-Stoikov), but on a ~300-name /
  8-day panel it may be too noisy to separate from the canary.
- **Secondary / the load-bearing one (confidence ~30%):** OFI's MARGINAL lift over the vwap_dev baseline is
  POSITIVE and exceeds the canary band — i.e. +OFI IC > baseline IC by a margin not explained by shuffle.
  I am genuinely uncertain here: vwap_dev reversion and OFI continuation may be the SAME information with
  opposite sign (a high-vwap-dev name that's still being bought may NOT revert), in which case OFI is an
  interaction/conditioner, not an additive carrier. A NULL marginal lift is a very plausible, honest outcome.
- **Pre-committed falsifier / KILL:** if OFI-only IC ≤ canary AND +OFI does not exceed baseline beyond the
  canary band, H2 (OFI as an additive cross-sectional carrier) is killed on this panel → "needs full-
  universe microstructure + more days, or OFI is a conditioner not a carrier." If +OFI clearly beats
  baseline beyond canary with a sane sign, H2 becomes a real lead → propose an OFI feature spec PR.
- **Honest caveat pre-committed:** ~300 names × 8 days is a small, survivorship-tilted panel; this probe can
  CONFIRM orthogonal signal or show it's absent at this scale, but a positive result must be re-run at full
  universe before any edge claim. Trades-based OFI on SIP is my OWN tick-rule sign, not the platform's
  parity-validated aggregation — sign agreement is ~99.8% per QA, so acceptable for a research read.
