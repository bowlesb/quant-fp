# W11 — Overnight-BETA premium (the survey's #1; a risk premium, NOT the W4 level) (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Lens L1/L7, from the literature survey's #1 pick
(Hendershott–Livdan–Rösch 2020, JFE). CRITICAL distinction from W4 (which we KILLED as survivorship): W4
tested the plain overnight RETURN level (an unconditional per-name level = survivorship). W11 tests the
overnight BETA premium — **high-beta names earn the market risk premium OVERNIGHT (close→open), low-beta
names intraday** — a CONDITIONAL, beta-sorted RISK premium documented to be large-cap-robust and structurally
hard to arbitrage (it's a risk-bearing compensation pattern, not a mispricing). The per-symbol demean that
killed W4 does NOT apply the same way: the signal is the beta-SORT × the overnight/intraday SPLIT, not a
per-name level.

## Hypothesis

Sorting LIQUID names by market beta, the HIGH-beta-minus-LOW-beta portfolio earns a positive premium realized
OVERNIGHT (close→open) and ~zero or negative INTRADAY (open→close) — i.e. the equity beta premium is an
overnight phenomenon. A portfolio that goes long high-beta / short low-beta held OVERNIGHT ONLY (buy at
close, sell at open) earns a positive net-of-cost return; the same beta tilt held INTRADAY does not.

## Universe + data
- /store/raw/bars (126 days — honest caveat: 126d is short for a stable beta estimate + a monthly premium;
  this is a PRELIMINARY test; the ≥18-month bar-depth ask makes it a real certification. Pre-commit: a
  126d result is "directionally suggestive", a deeper one certifies.). LIQUID = top ~500 by dollar-volume.
- Market = SPY (or an equal-weight liquid-universe proxy). BETA per name = rolling regression of the name's
  daily return on the market return over a trailing window (e.g. 60-day); re-estimate monthly.
- Per (symbol, date): overnight = open/prev_close − 1; intraday = close/open − 1.

## Test design
1. Each rebalance (monthly / every 21d): sort liquid names into beta quintiles. Form the high-minus-low-beta
   L/S portfolio.
2. Realize the L/S return THREE ways: (a) OVERNIGHT-only (long high-beta@close→open, short low-beta), (b)
   INTRADAY-only, (c) 24h (full day). The hypothesis: (a) > 0, (b) ≤ 0, and (a) is where the premium lives.
3. Cost: overnight bet = one round-trip per holding period at the measured spread (low turnover — beta is
   slow-moving, so the quintile membership changes little → LOW turnover, friction-favorable). Charge
   measured spread on leg changes + the MOC/MOO auction caveat + 2× stress.
4. GATES: shuffle-canary (permute beta→overnight-return); walk-forward OOS; per-rebalance bootstrap on the
   non-overlapping overnight L/S returns (CI excludes zero above); demean is NOT the primary kill here (the
   signal is conditional on beta, not a per-name level) but report a beta-residualized version as a robustness
   check. DECISIVE: OOS overnight high-low-beta L/S net-of-cost, bootstrap CI > 0, AND the
   overnight-vs-intraday SPLIT is in the predicted direction (overnight > intraday).

## Expected / confidence
- Confidence the overnight-beta premium shows the predicted split AND clears net-of-cost OOS: **~35%** — the
  survey's top pick because it's (a) documented large-cap-robust, (b) a RISK premium (structurally durable,
  not arbitraged like a mispricing), (c) LOW turnover (beta is slow), (d) the overnight/intraday split is a
  sharp, falsifiable prediction. The main risk: 126d is thin for beta + a monthly premium (hence the bar-depth
  ask), and beta is itself noisy. Pre-commit the prior.
- KEEP-AS-LEAD: OOS overnight high-low-beta L/S net positive, bootstrap CI > 0, the split in the predicted
  direction, canary-clean → a low-turnover overnight-beta paper container + certify on ≥18mo bars. AMBIGUOUS:
  split present but cost-marginal on 126d → "promising, certify on deeper history". KILL: no split / overnight
  L/S net ≤ 0 OOS.

## Friction-wall scorecard
[risk-premium ✓ durable] [low-turnover ✓ beta is slow] [liquid ✓] [portfolio ✓ beta L/S] [overnight/intraday
split = a sharp falsifiable test, not a fishing expedition] — the single most friction-favorable + structurally
durable bet the survey surfaced. NOT a re-run of W4 (that was the level; this is the conditional beta premium).
