# Roadmap — the Manager's milestone ladder (owned by the Manager, re-read every wake)

This is the single source of truth for **where we are going, what we are driving toward NOW,
and how every agent's work ladders up to the goal.** The Manager keeps it current and
communicates it in both directions (see Communication Protocol below). Every other role reads
the CURRENT MILESTONE at wake and frames its work against that milestone's exit criteria.

Dates are real-world targets, not promises — when one slips, the Manager moves it and says why.
Exit criteria are quantifiable: a milestone is DONE only when its checks are objectively green.

---

## North star (the 10,000-ft vision — judge everything against it)
A trustworthy, maintainable, bug-free automated trading **platform** that runs 24/7, finds real
edge through many cheap honest shots, and **eventually makes money** — paper-first with hard
statistical gates, then real capital scaled by a proven track record (Ben will commit up to
~$100K of real money *after* paper proves out). Honesty over speed: a false edge is worse than
no edge. Strategy: cross-sectional short-horizon ML ranking over ~1000 liquid US equities.

The end state: a self-correcting company-in-a-box that researches → validates → deploys edges
under hard gates, scales capital as the track record justifies, and never fools itself.

---

## Milestone ladder (dated + quantifiable)

### M0 — Execution lifecycle proven live ✅ DONE (2026-06-11)
Full bet lifecycle on a real market day: submit (NBBO marketable-limit) → fill → manage
(fills/reconcile/pnl) → **terminate (EOD flatten, broker confirmed flat)**.
Exit criteria (all met): live paper order round-trip; EOD flatten leaves 0 positions/0 open
orders; P&L recorded. *Status: complete.*

### M1 — Trustworthy data & a CLEAN research panel 🔴 NOW (target 2026-06-13)
The edge verdicts are only as honest as the panel. We found the ~600-day panel was ~21% ETFs/
leveraged funds; the price-only "no edge" verdict is therefore suspect.
**Exit criteria (quantifiable):**
- [ ] Universe = single-name equities ONLY — **0 ETF/leveraged/fund members** (automated
      invariant `universe_is_equities_only` green in the QA suite & CI).
- [ ] Clean equity panel rebuilt over the full ~600-day history, PIT-correct — ~885-900
      equities/date (rebuild found removing funds UN-CROWDS ~160 equities/date the 1000-cap
      had displaced; labels recomputed too, not just features).
- [ ] Price-only cost-gated battery **RE-RUN on the clean panel**; verdict re-validated and
      documented (trustworthy, not contaminated) in STATE/EXPERIMENTS.
- [ ] All QA invariants are **automated checks** (calendar/DST, parity, PIT, warmup, no-Inf,
      universe-composition), each fail-loud — not prose.

### M2 — Order-flow data at scale (target 2026-06-20)
Order flow is the most plausible remaining edge source at our latency. It is wired end-to-end
but only covers ~50 names.
**Exit criteria:**
- [ ] Trade/quote capture scaled 50 → **≥500 liquid equities** (shard when one process can't
      keep up), ingestor stable a full session.
- [ ] Settled-day trade-agg parity **≥98%** at scale (QA invariant I2b green on a settled day).
- [ ] Order-flow features (v1.2.0+) populate across the wide cross-section (NaN-rate < 5% where
      coverage exists).

### M3 — First HONEST edge candidate (target 2026-07-15)
**Exit criteria (a candidate must pass ALL, on clean data):**
- [ ] Within-timestamp rank-IC with **Newey-West t > 3**.
- [ ] **Clean shuffle-label canary** (no leakage).
- [ ] **Positive net-of-cost** L/S backtest (breakeven bps > realistic cost at our turnover).
- [ ] Survives **survivorship neutralization** (per-symbol demean → timing alpha, not survivors).
- [ ] If no candidate passes: documented honestly; feature/data iteration continues. NO false edge.

### M4 — Paper track record at scale (target 2026-09-01, gated on M3)
**Exit criteria:**
- [ ] Edge candidate run live-paper **≥20 trading days** with daily stat-gate monitoring.
- [ ] Realized net Sharpe & IC consistent with backtest within tolerance; no risk-limit breach.
- [ ] Settled-day reconciliation muscle exercised (fills/fees vs broker records, Exec/Risk owns) —
      mandatory before M5 real money. (Added 2026-06-11 from Exec/Risk coverage question.)

### M5 — Real capital, small → scaled (target 2026-10-01, gated on M4, Ben signs off)
**Exit criteria:**
- [ ] Deploy small real money (start ~$5–10K); scale toward ~$100K strictly per proven Sharpe +
      drawdown discipline. Ben approves the go-live and each scale-up.

---

## Current focus (the Manager updates this line every wake)
**Driving toward M1.** Immediate: (1) supervised clean-universe rebuild (the `is_etf_like` fix is
committed and inert — run `build-universe-history`), (2) re-run the price-only battery on the clean
panel, (3) stand up the automated QA invariant suite (starting with `universe_is_equities_only`).
M2 (order-flow scaling) proceeds in parallel as data/throughput allow.

---

## Communication protocol (both directions — this is half the Manager's job)

**DOWN (Manager → team), every wake:** post the CURRENT MILESTONE + its exit criteria + each
role's specific assignment toward it, so every agent knows how its task ladders up. No role works
on anything without knowing which milestone criterion it advances.

**UP (Manager → Ben), every cycle:** a concise status — which milestone, quantifiable progress
(which exit criteria are green / what remains), blockers, decisions that need Ben, and the exact
next resume time. Escalate anything that needs Ben's call (e.g. M5 go-live, risk changes).

**LATERAL (teammate ↔ Manager):** teammates develop their own domain context, raise cross-lane
concerns ("is anyone owning X?"), and **ask the Manager questions**; the Manager answers and
re-assigns. Gaps between roles are the Manager's to catch. Evidence, not vibes — "green" means a
check was actually run this cycle.
