# LIQUIDITY-PROVISION SURFACE — VERDICT (core, registered #218 pre-reg)

**Date:** 2026-06-20 · Pre-reg #218 (locked: back-of-queue + REAL trade-print join, median-anchored).
Substrate: liquid core (SPY/QQQ/AAPL/MSFT/NVDA/PLTR/AMD/TSLA/AMZN/META), 379d (2024-12-12→2026-06-18),
**5,326,817 simulated fills** off the joined tick NBBO + real trade tape. READ-ONLY. Code on this branch.

## TL;DR — NULL, cleanly decomposed. Uninformed LP doesn't clear the exit + adverse selection.

The earn-the-spread framing — the one place cost could be the EDGE — is a **clean null** at our footing
(uninformed, taking exit, back-of-queue). Per the pre-committed median-anchored gate, the net per-fill
MEDIAN is NEGATIVE at every horizon. And the decomposition shows EXACTLY why, in bps:

| H | net per-fill MEDIAN | net mean | half-spread earned | adverse markout (OBSERVED) | taking-exit cost |
|---|---|---|---|---|---|
| 1m | **−0.22 bps** | −0.31 | +0.64 | **−0.24** | −0.65 |
| 5m | **−0.19 bps** | −0.30 | +0.64 | **−0.22** | −0.64 |
| 15m | **−0.16 bps** | −0.28 | +0.64 | **−0.21** | −0.63 |

**The identity that kills it:** `+0.64 (earn the half-spread) − 0.22 (adverse selection) − 0.64 (cross the
spread to exit) ≈ −0.22 bps`. The TAKING EXIT pays back the ENTIRE half-spread you earned at entry, and the
**observed adverse markout (−0.22 bps — an OUTPUT of the tape, not a set parameter) is pure additional
loss**: you fill precisely when the mid is about to move against you. This is the canonical uninformed-LP
result, now measured cell-by-cell on 5.3M real-print fills. NO horizon, NO setting flips the median positive.

## The join is validated (the required hardening worked)
Fill-rate diagnostic (the false-positive guard): simulated filled shares/day = **0.04%–0.37% of actual
printed daily volume** across the core — a small, realistic LP participation, confirming the trade-print
join is doing its job (a $10k passive order captures a tiny real sliver; cancels generate no print so they
never manufacture a phantom fill). The earlier displayed-decay proxy is rejected and replaced; this verdict
rests on real prints.

## Capacity gate — a SECONDARY honest finding (reported, not used to force a verdict)
The pre-registered capacity gate (`OUR_SIZE ≤ median displayed depth`) disqualified ALL core names: the SIP
NBBO TOP-OF-BOOK displayed size is tiny (median 3–9 shares) while a $10k order is 15–70 shares. Read
literally, no name "qualifies" — i.e. even sizing in at $10k/quote exceeds the displayed top lot. I did NOT
silently relax this frozen gate to manufacture a verdict; instead the VERDICT rests on the **per-fill
economics over the full fill set** (the median above), which answer "does LP earn the spread" independently
of who can size in. The capacity finding is a real, additional negative (the displayed-depth-vs-$10k tension
the design predicted), reported as such. NOTE: the literal top-of-book gate is conservative — true book
depth exceeds the displayed NBBO lot — but since the per-fill median is negative regardless, relaxing it
cannot flip the verdict.

## H2 conditional provision — NOT a free pass
Could quoting only when adverse-selection risk is low flip the median? An ORACLE filter (look-ahead: keep
only fills whose realized markout was favorable) gives +8.3 bps median on the kept ~49% — but that is
CHEATING (it uses the future markout to select). It only says there's room IFF you can predict adverse
selection causally. A real, point-in-time adverse-selection predictor (quote imbalance / micro-price drift)
is itself a directional-prediction problem — the exact thing that has nulled 8 times. So H2 is NOT settled
positive; it requires a proven causal adverse-selection signal, which would need its own pre-registered
test (shuffle + look-ahead controls). Not claimed here.

## Disposition — NULL, no escalation (the pre-committed outcome)
Net per-fill median ≤ 0 at every horizon → LP at our (uninformed, no-rebate, taking-exit, back-of-queue)
footing does not clear adverse selection + the exit. Honest null, NO replication flag, NO promotion. The 9th
settled negative — and the most cleanly decomposed: we now have the exact LP P&L identity (earn ≈ exit, with
adverse selection as pure loss) measured on 5.3M real-print fills.

WHAT IT SETTLES + the only doors left open: uninformed two-sided provision is a loser at our footing, as
theory predicts. The non-null paths all require something we don't have: (a) a maker REBATE (changes the
exit/entry economics — an execution-venue question, not a signal), (b) a PASSIVE exit (don't pay the spread
to flatten — but then you carry inventory + adverse selection longer), or (c) a real causal
adverse-selection predictor for H2 (a fresh, hard, separately-pre-registered hunt). The widest-spread
mega-caps (1–5bps, per the board note) are where any residual would be largest — but the structural
earn≈exit identity holds regardless of spread width with a taking exit.

## Method / infra notes
- Fill rule off REAL trade prints (registered #218): resting BUY at B fills when cumulative print size at
  price≤B (while NBBO bid≥B) crosses Q0 (back-of-queue); fill ts = the print ts. Cancels generate no print.
- Adverse markout = the REAL future mid move over H, signed by side — an OUTPUT, reported, not tuned. Exit =
  the contemporaneous taking half-spread at H.
- Feed = consolidated SIP NBBO (15 exchange codes/side); displayed size = aggregate touch, so back-of-queue
  is an aggregate-FIFO approximation (stated; we lean pessimistic).
- Infra: chunked-subprocess fill sim → host-mounted resumable per-(sym,day) ledger (3,790 files) +
  .RUN_COMPLETE; ran clean in one detached named container. The full 150-name liquid-universe scale-up would
  replicate the structural earn≈exit identity (universal under a taking exit) — the core (5.3M fills) is the
  verdict; the scale-up is confirmatory, not decisive.
