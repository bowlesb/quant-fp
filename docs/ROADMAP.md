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

### M1 — Trustworthy data & a CLEAN research panel ✅ DONE (2026-06-12, a day early)
The edge verdicts are only as honest as the panel. We found the ~600-day panel was ~21% ETFs/
leveraged funds; the price-only "no edge" verdict is therefore suspect.
**Exit criteria (quantifiable):**
- [x] Universe = single-name equities ONLY — **0 ETF/leveraged/fund members** (automated
      invariant `universe_is_equities_only` green in the QA suite & CI). ✅ 2026-06-12:
      614 dates / 455,881 members / 0 violations; invariant FAILED on dirty fixture, PASSES
      0/1000 clean; independent `universe_no_known_funds` (5,284-name frozen denylist) also green.
- [x] Clean equity panel rebuilt over the full ~600-day history, PIT-correct — **~715
      equities/date** (~715-742 range, ~742 avg) as set_version **v1.1.1** (labels
      recomputed too, DELETE-then-insert). ✅ 2026-06-12: 5,525,040 rows / 613 dates /
      785 symbols / 2024-01-02→2026-06-11; NaN 0.000% on all 21 features; labels fwd_30m
      4.84M + fwd_60m 4.42M (613d) + overnight 428K (600d, ~2% month-boundary gaps);
      computed_at acceptance gate passed.
      (CORRECTED 2026-06-12: an earlier "~885 with ~160 un-crowded equities" reading came
      from a stale-image rebuild that ran pre-fix code; contamination was purely ADDITIVE
      ETFs — old ~933/date ≈ ~210 funds + ~723 equities. Note: v1.1.0 feature rows stay
      frozen but their original labels are overwritten — v1.1.0 must NEVER be re-batteried;
      its canonical results live in experiments/results.jsonl.)
- [x] Price-only cost-gated battery **RE-RUN on the clean panel**; verdict re-validated and
      documented (trustworthy, not contaminated) in STATE/EXPERIMENTS. ✅ 2026-06-12: all 8
      configs NO EDGE on clean v1.1.1 (30m: IC 0.027-0.032, clean canary, net-NEGATIVE,
      breakeven ~1.4bps < ~2bps cost; overnight: survivorship, neutralized sharpe ≤ −0.35).
      Robust to the split-discontinuity caveat: 11-name sensitivity pass moved nothing beyond
      rounding (e.g. 30m raw IC 0.0270→0.0266). Pre-registered ~70% prediction held. Price-only
      ENDPOINT; path to edge = order-flow + delisted backfill.
- [x] All QA invariants are **automated checks** (calendar/DST, parity, PIT, warmup, no-Inf,
      universe-composition), each fail-loud — not prose. ✅ 2026-06-12: scripts/qa_invariants.py,
      10 invariants, CI-able (3f478d7). 9 green / 1 deliberately RED: backfill↔realtime bar
      parity 1.14% vs 1% gate — real divergence under drill (task #14), gates M2 scale-up.

### M2 — Order-flow data at scale 🔴 NOW (target 2026-06-20)
Order flow is the most plausible remaining edge source at our latency. It is wired end-to-end
but only covers ~50 names.
**Exit criteria:**
- [ ] Trade/quote capture scaled 50 → **≥500 liquid equities** (shard when one process can't
      keep up), ingestor stable a full session.
- [ ] Settled-day trade-agg parity **≥98%** at scale (QA invariant I2b green on a settled day).
- [ ] Order-flow features (v1.2.0+) populate across the wide cross-section (NaN-rate < 5% where
      coverage exists).
- [ ] **Research universe == live tradable universe**: backfill history for the ~150 fixable
      partial-history live-universe names (CORRECTED 2026-06-12: 750/1000 full, 250 partial,
      0 zero-history; PIT membership already self-corrects, so this is breadth insurance, not
      a validity bug; ~100 are post-2024 listings with nothing to fetch). Cheap: ~1GB /
      <90min via existing backfiller (scope memo docs/BACKFILL_SCOPE.md, task #12).

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
**M1 DECLARED DONE 2026-06-12 (a day early) — driving toward M2 (order-flow at scale, 6/20).**
Tonight's post-close batch (prod owns, ONE ingestor restart): #17 KLAC re-fetch, #11 structural
stale-image fix, clean-membership pickup, then #12 backfill + #16 model train/review/swap (outside
RTH) + QA's #15 first full-50-name settled-day parity proof. M2 work: sharding design with live
coverage invariant, capture 50→500, #10 v1.2.0 OFI panel, #18 CA feed, OFI pilot ~6/26 (gated on
#15). Standing reds under drill: bar-parity 1.14% (#14).

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
