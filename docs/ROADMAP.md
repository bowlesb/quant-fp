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
      ◐ 2026-06-12: PROVEN at current 50-name scale on the first true full-session settled day —
      count parity 99.79% / sign agreement 99.85% / signed_vol 99.41% over 36,334 overlap minutes
      (only the 16:00 auction hour dips, excluded from OFI by design). The INVESTMENT gate for
      500-name sharding is MET; criterion ticks when re-proven at ≥500. Standing rule: OFI ≤15:59
      ET (backfill trade-agg is RTH-bounded — no validation reference after the close).
- [ ] Order-flow features (v1.2.0+) populate across the wide cross-section (NaN-rate < 5% where
      coverage exists).
- [x] **Research universe == live tradable universe**: backfill history for the ~150 fixable
      partial-history live-universe names (CORRECTED 2026-06-12: 750/1000 full, 250 partial,
      0 zero-history; PIT membership already self-corrects, so this is breadth insurance, not
      a validity bug; ~100 are post-2024 listings with nothing to fetch). Cheap: ~1GB /
      <90min via existing backfiller (scope memo docs/BACKFILL_SCOPE.md, task #12).
      ✅ 2026-06-12: 43.6M bars upserted for 222 thin names; universe depth 988/1000 ≥120d
      (residual 12 are genuinely-young listings — all available history fetched). Achieved at
      the BAR level; the v1.1.2 full-universe panel (785→988 names) ticks the panel level
      whenever sequenced (v1.1.1 stays frozen as the M1 verdict panel).

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

## Process milestones (org-maturity ladder — Ben's directive 2026-06-12)

Quantifiable milestones (M-series above) measure WHAT the system achieves. These P-series
milestones measure HOW the org works — the behaviors that make the M-series results
trustworthy. The Manager reports position on BOTH ladders whenever Ben asks. Status values:
ACHIEVED (demonstrated, now must be maintained) / PARTIAL / NOT STARTED. An ACHIEVED process
milestone can REGRESS — the Manager re-checks these, not just the M-series, every wake.

### P1 — Evidence-first culture: claims are verified, not asserted ✅ ACHIEVED (maintain)
- [x] Every "done" is backed by a check that was actually RUN this cycle (e.g. universe
      rebuild verified 0-funds-by-query; #13 verified by container grep; exec's "flat"
      always from a fresh broker snapshot).
- [x] Self-corrections happen BEFORE wrong numbers are consumed downstream (displacement
      finding retracted pre-battery; QA re-attributed KLAC direction; scope corrected
      ~285→0 zero-history names).
- [x] Pre-registration: hypotheses written down blind, before results (battery predictions
      ~70% no-edge — held; breadth tripwire bound exactly as pre-registered).
- Maintenance bar: any claim that reaches Ben or a verdict traces to a run check.

### P2 — Flaws are caught by the SYSTEM, not by luck 🟡 PARTIAL
- [x] Automated invariants catch a real issue within their first day (10× KLAC corruption
      found by the parity suite, 2026-06-12).
- [x] Detection→root-cause→structural-fix loop completes without Ben intervening (KLAC:
      drill → reversal → CA-API ground truth → adjustment-consistency fix + new invariant).
- [ ] SELF-HEALING live: CA-feed auto-re-fetch wired and proven on the NEXT real corporate
      action (coded 5f17db9; activates at tonight's rebuild; proven only when a future
      split is handled with zero human attention).
- [ ] QA's unprovoked creative probing is a sustained habit (≥1 novel probe per wake,
      logged even when clean) — re-instated 2026-06-12 after drift; needs a week of streak.

### P3 — Modeller as an independent research engine 🟡 PARTIAL
- [x] Ships honest negatives under pressure — twice declared "no edge" on work the org
      wanted to succeed, gates intact; survivorship/canary discipline never bent.
- [x] Independent method development: 4-gate battery harness built as ONE deterministic
      command; sensitivity methodology self-designed.
- [ ] Always-running exploration: experiment queue non-empty ≥95% of days; 2-4 curiosity
      long-shots/day logged (restarted 2026-06-12 after going cold; needs a sustained streak).
- [ ] Full new-feature loop completed END-TO-END at least once: invent idea → drive data
      collection (with Prod) → parity-verify (with QA) → test on real panel → keep/discard.
      (OFI is mid-loop: features built, data accruing, pilot pre-registered ~6/26.)
- [ ] A modeller-initiated feature idea reaches production scoring (not just the panel).

### P4 — Operational steadiness (quantifiable, daily) 🟡 PARTIAL
- [x] One full RTH session: collection uninterrupted, zero unplanned restarts, live trading
      lifecycle clean (2026-06-12 is the first candidate; settled-data verification tonight).
- [ ] 5 consecutive such days (counter resets on any unplanned restart/gap/red invariant
      that isn't known+owned).
- [ ] 20 consecutive days (aligns with M4's paper-track requirement).
- Daily definition of "smooth": bars landing every minute all session; trade/quote capture
      complete vs subscription; reconcile ok; EOD flatten confirmed; QA suite green-on-active.

### P5 — Org continuity survives infrastructure failure ✅ ACHIEVED (maintain)
- [x] Session death → ledger-based respawn with no lost state, no repeated work (3 role
      respawns on 2026-06-12: prod-architect→-2 with written handoff; modeller-2 + qa-2
      from EXPERIMENTS.md / QA_LEDGER.md).
- [x] Every decision journaled + committed same-cycle; the board (tasks) is the
      authoritative record of rulings, robust to message mislabeling.
- Maintenance bar: any new role/process must write its durable state to a ledger from day 1.

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
