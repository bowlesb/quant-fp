# PRE-REGISTRATION — Implied-vol vs the trailing-vol PROXY (the load-bearing assumption behind #331's vol-lane shelve)

**Date:** 2026-06-21  **Author:** Modeller  **Status:** PRE-REGISTERED before any IV-vs-forecast number.
**Lane:** vol/magnitude — narrow follow-up to #331 (the merged vol-edge G0). NOT a new alpha hunt.

---

## 0. Why this experiment exists (the gap it closes)

#331 ran the vol lane and reached a clean two-part result:
1. The vol signal is **real and incremental-over-persistence** (vol term-structure `realized_vol_60m`
   incremental rank-IC +0.243, NW t +10.6 over the trailing-vol baseline; shuffle-clean).
2. The straddle net-of-cost G0 was a **forecast-edge NULL**: the conclusion was *"vol is so persistent it
   is (efficiently) priced into the premium, leaving no forecast-driven $ after cost."*

**The load-bearing flaw:** conclusion (2) was produced against a **trailing-vol PROXY premium**
(`premium ≈ 0.8 · trailing_rv · sqrt(H)`, `g0_straddle.py` PREMIUM_K=0.8). The option premium was NEVER
real option-implied vol — it was *assumed equal to trailing vol*. So "vol is efficiently priced into the
premium" is an **assumption baked into the proxy**, not a measurement. If real option-implied vol diverges
from `0.8·trailing_rv·sqrt(H)` in a way our forecast predicts, the proxy-straddle G0 mechanically could
not have seen it (its "premium" moved one-for-one with the trailing-vol basis of the forecast).

Alpaca options ARE accessible with per-contract IV + greeks (VERIFIED this cycle: SPY 13,349 contracts /
12,004 with IV; NVDA 4,106 / 2,653 with IV; greeks delta present; OCC symbol encodes expiry+strike+type).
**IV is a CURRENT snapshot only — no history.** So the valid, no-new-backfill test is a single-snapshot
CROSS-SECTIONAL screen, NOT a forward backtest. Scope is explicitly G0 (a screen to decide shelve-vs-build),
not a tradeable verdict.

---

## 1. The hypothesis (pre-committed)

**H0 (efficiently-priced, the #331 implicit assumption):** across the liquid cross-section, ATM option-
implied vol is explained by trailing realized vol; our incremental-over-persistence forecast carries **no**
systematic information about the implied-vs-trailing residual. ⇒ the proxy was adequate, the lane shelves.

**H1 (proxy masked a real signal):** ATM implied vol systematically **diverges** from the trailing-vol
proxy, AND the divergence is **predicted by our forecast** with a consistent sign — i.e. on names where our
forecast says forward vol > trailing vol, the market's IV does NOT fully reflect that (or over-reflects it).
⇒ the proxy-straddle G0 was blind to a real implied-vs-forecast spread; motivates an option-IV backfill for
a proper point-in-time backtest.

Either outcome is publishable and decision-relevant. H0 closes the lane honestly; H1 re-opens it with a
concrete, bounded next dependency (historical option-IV backfill — Ben/DI decision).

---

## 2. Construction (single snapshot, cross-sectional)

**Universe:** the liquid head — top-N by recent ADV from the store raw-bar panel (N target ≈ 100–150;
whatever has both a queryable option chain with IV and clean recent bars). Each name contributes ONE row.

**Per underlying, measured AS-OF the snapshot minute T (all point-in-time, no look-ahead):**
- `atm_iv` — ATM implied vol from the option chain: pick the nearest expiry ≥ ~5 calendar days and ≤ ~45d
  (to bracket a 30-min..multi-day horizon; IV is an annualized number, horizon-robust as a *level*), then
  interpolate IV at-the-money in strike (the two strikes bracketing spot, or the |delta|→0.5 contract).
  Average the call & put ATM IV. Require a valid `latest_quote` on the chosen contracts (no stale-only).
- `trail_rv_ann` — trailing realized vol from recent contiguous 1-min bars, **annualized** to match IV units
  (std of 1-min log-returns × sqrt(minutes-per-trading-year)). This is the proxy basis #331 used.
- `forecast` — the #331 incremental-over-persistence winner computed point-in-time from recent bars:
  primary = `realized_vol_60m`-style longer-window trailing vol (the term-structure term that beat the
  30-min baseline incrementally), annualized. Secondary (robustness) = a small read of the other #331
  incremental survivors (`spread_bps`, `book_depth`) — reported, not added to the primary gate.

**Units discipline:** everything compared in the SAME annualized-vol units. IV is already annualized;
realized vols are annualized with the same minute-count convention. The proxy basis is `trail_rv_ann`.

---

## 3. The tests (pre-committed, in order)

**T1 — does trailing vol alone explain IV? (the proxy-adequacy check)**
Cross-sectional rank-IC and OLS of `atm_iv` on `trail_rv_ann`. Report R², slope, and the residual
`iv_resid = atm_iv − fitted`. (Descriptive — sets up T2; high R² alone does NOT decide the question.)

**T2 — does the forecast predict the IV residual? (THE decision test)**
Cross-sectional **rank-IC of `forecast` vs `iv_resid`**, with:
- a within-snapshot **SHUFFLE** null (permute `forecast` across names; leakage/spurious canary),
- the **incremental** form: rank-residualize BOTH `forecast` and `iv_resid` on `trail_rv_ann` again
  (forecast is partly trailing-vol; we want the part of the forecast independent of the proxy basis
  predicting the part of IV independent of the proxy basis), report incremental rank-IC + collapse.
- sign must be **consistent** with a real story (forecast-high-vol ⇒ IV-under-prices, or the reverse —
  whichever, it must be one consistent direction, reported, not cherry-picked post hoc).

**T3 — robustness across the snapshot structure (anti-fooling):**
- Repeat T2 at a 2nd expiry bucket (near vs ~1-month) — the sign must not flip.
- Repeat with the put-only and call-only ATM IV (the average shouldn't be carried by one wing).
- Report N (names) and the per-name IV/trail dispersion (a degenerate near-constant cross-section is a
  no-decision, not an H0 confirmation).

---

## 4. Decision gate (pre-committed, strict — a G0 screen, not a tradeable claim)

**H1 (re-open the lane → motivate the option-IV backfill) requires ALL of:**
- T2 incremental rank-IC |≥ 0.10| (a real cross-sectional residual signal, not a whisper),
- shuffle-clean (|shuffle IC| < ~0.03 and edge-vs-shuffle ≥ 0.07),
- sign-consistent and **stable** across T3 (both expiry buckets same sign, both wings same sign),
- N ≥ 60 names with non-degenerate IV/trail dispersion.

**Anything short of ALL of the above = H0 confirmed = the vol lane SHELVES** (the proxy was adequate;
"efficiently priced" stands as a measurement now, not an assumption). I will NOT relax the gate post hoc;
a near-miss is a shelve, recorded with the numbers.

**Hard honesty caveat (stated up front, cannot be argued away by a positive result):** this is a SINGLE
SNAPSHOT. Even a clean H1 is NOT a tradeable edge — it is a *screen result* that says "a forward, point-in-
time implied-vs-forecast study is worth the option-IV backfill." A single-snapshot cross-sectional residual
can reflect a stable IV-surface artifact (sector/beta/expiry-listing structure) rather than a forecastable
mispricing; the backfill + a forward purged walk-forward is the only thing that could ever certify $. So
even the best outcome here recommends a *backfill + proper study*, never a paper-trade.

---

## 5. Discipline / ops

- Pre-registered BEFORE any IV-vs-forecast number (the chain probe — contract counts / IV presence — was a
  pure access check, no forecast involved).
- Research scratch only: `experiments/2026-06-21-vol-implied-vs-proxy/`. NO PR, NO quantlib/live-tree edit,
  NO fingerprint/deploy.
- Bounded `--rm` fp-dev, `/store` read-only, single-threaded snapshot read. Host load is high (~16) — the
  option-chain pulls are network-bound and light; I will NOT starve live capture (no fc/strategy/crypto
  touch, no docker kill/restart).
- Reuse the existing IC machinery (`quantlib.backtest` per_timestamp_ic / mean_ic / newey_west_tstat /
  shuffle_within_groups) — but note: a single snapshot = ONE "timestamp" group, so the cross-sectional IC
  is a single-snapshot rank correlation; NW-t over snapshots is not available (one snapshot). I therefore
  report the single-snapshot rank-IC + a bootstrap CI over names + the shuffle null, NOT an NW-t (honest:
  no time dimension in a snapshot). If a 2nd snapshot is cheaply available (a few hours apart) I take it as
  a 2nd independent draw for sign-stability, NOT to manufacture a t-stat.
