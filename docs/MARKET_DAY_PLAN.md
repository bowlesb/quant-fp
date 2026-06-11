# Market-Day Plan — the pre-open ritual

Who owns preparing for the open? **The Manager produces this plan before each session**,
synthesizing three owners' inputs. It answers: what are we trying to ACHIEVE today, is the
system READY, and what is the trade-path GO/NO-GO.

## Owners of the inputs
- **Manager — OBJECTIVES:** given the north star + where we are, what is today's goal?
  (At our stage: validate the live loop + collect/deepen data + honest experiments — NOT
  "make money," because we have no proven edge yet. Say so plainly.)
- **Production Engineer — OPERATIONAL READINESS:** services up; live ingestion healthy;
  TODAY's universe_membership built pre-open; model loaded; stale-data auto-halt armed;
  backfill-manager throttled for RTH; disk/DB headroom.
- **Execution/Risk — TRADE-PATH GO/NO-GO:** DRY_RUN state (true until there's edge);
  caps + kill-switch state; reconciliation live; what the executor WILL do at the open.

## Pre-open checklist (Manager verifies, with evidence)
- [ ] all services up; ingestion fresh (last bar age < 2 cadences)
- [ ] today's universe_membership exists (model-server won't fall back to a stale set)
- [ ] model + meta loaded; predictions table reachable
- [ ] stale-data halt + score-degeneracy + staleness guards active
- [ ] executor mode confirmed (DRY_RUN=true unless a signal has cleared the gates)
- [ ] reconciliation_log writing (broker-truth probe alive)
- [ ] backfill throttled during RTH so the open burst isn't starved

---

## Plan for 2026-06-11 (open 09:30 ET / 06:30 PDT)
- **Objective (Manager):** VALIDATION + DATA, not trading. (1) Confirm the model-server
  fires its FIRST real autonomous cadence at the open (it never has in prod). (2) Confirm
  the dry-run executor forms + logs a basket from fresh predictions (no submit). (3) Keep
  the deep backfill + live collection running. Do NOT trust the first 1-2 cadences' deciles
  (NaN 60m features near the open). NO real trading — no edge exists.
- **Operational readiness (PE):** verify today's universe is built pre-open; watch the
  09:30/10:00 ET cadences in model-server logs; ensure backfill throttle engages in RTH.
- **Trade-path (Execution/Risk):** DRY_RUN stays TRUE. Watch the executor reject stale
  preds pre-open, then form an (unsubmitted) basket once a fresh cadence lands; confirm
  reconciliation stays live and broker stays flat.
- **Go/No-Go:** GO for validation; NO-GO for any order submission (gated on proven edge).

## 2026-06-11 — RESULT (end-of-day record)
- VALIDATION PASSED autonomously at the open: model-server fired first real cadences (988/981);
  dry-run executor logged baskets.
- Went LIVE (paper) mid-session (Exec/Risk GO-WITH-FIXES): full bet lifecycle proven on a real
  market day — submit (NBBO marketable-limit) -> fill (6-leg basket) -> manage (fills_log captured,
  reconcile ok, pnl_daily tracked) -> TERMINATE (EOD flatten ~15:48 ET, verifying).
- Live exercise found+fixed 4 real bugs (stale-close pricing, mode/traded_today re-submit loop,
  dup-coid guard, fills-capture). Day P&L ~ -$1.20 (tiny noise; NO edge — execution-infra proof).
- Edge track: deep ~600-day panel rebuilding in parallel for the first honest overnight test.
