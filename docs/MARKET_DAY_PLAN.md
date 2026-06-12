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

## 2026-06-11 — TERMINATION VERIFIED (full lifecycle proven)
EOD flatten fired 15:48 ET: "closing 6 positions + cancelling open orders" -> broker FLAT
(0 positions, 0 open orders) confirmed. Realized day P&L -$10.07 (tiny noise; NO edge). The
FULL bet lifecycle is now proven on a live market day: submit (NBBO) -> fill (6 legs) -> manage
(fills_log + reconcile + pnl_daily) -> TERMINATE (EOD flatten). Execution infrastructure COMPLETE
+ validated. Bets do not linger. (Edge separate: price-only proven dead; order-flow next.)

## Plan for 2026-06-12 (open 06:30 PDT)
- **PRIORITY #0 — ETF CONTAMINATION (overnight finding, supervised):** ~207 of 1000 universe
  members (~21%) are ETFs/leveraged-inverse/VIX-futures funds (SOXL, TQQQ, SQQQ, UVXY, VXX,
  UPRO...), NOT single-name stocks. They were RANKED cross-sectionally against stocks in the
  1.59M-row feature panel -> the price-only "NO EDGE" verdict was drawn on a ~21%-contaminated
  cross-section and is NOT trustworthy. ACTIONS (supervised, in order): (a) review the exclusion
  set (scripts/etf_exclusion.sql); (b) exclude funds from universe_membership; (c) rebuild a CLEAN
  equity-only panel; (d) RE-RUN the price-only cost-gated battery on clean data — does "no edge"
  hold, or did contamination mask/distort it? This gates everything: don't trust ANY edge verdict
  (price-only OR order-flow) computed on the contaminated cross-section. The order-flow scaling
  list must also be clean (staged: scripts/etf_exclusion.sql -> top-200 equity-only, ETFs removed).
- Objective: (1) ORDER-FLOW validation — 50-symbol trade/quote throughput holds a full session +
  settled-day trade-parity (backfill yesterday's aggs once settled + validate-aggs); (2) keep the
  EXECUTION lifecycle exercised — executor stays LIVE tiny paper (DRY_RUN=false) to catch
  regressions + keep proving submit->manage->terminate daily (P&L is noise; no edge). 
- Readiness (PE): universe + today's membership pre-open; ingestor streaming 50-sym trades/quotes
  + universe bars; model-server scores; backfill throttled in RTH.
- Trade-path (Exec/Risk): live tiny paper; EOD flatten must terminate again (proven yesterday).
- Go/No-Go: GO for tiny-paper exercise + order-flow validation; NO real-size trading (no edge).

### Execution/Risk — TRADE-PATH GO/NO-GO (verified 2026-06-12 ~00:00 ET, fresh evidence)
**VERDICT: GO for the tiny live-paper lifecycle exercise. NO-GO for any size-up / real money.**
Closed-loop verification — every line RAN this cycle, not asserted:
- **Book is FLAT (fresh broker snapshot):** 0 positions, 0 open orders; equity $100,027.22 ==
  cash (fully de-risked). Account ACTIVE, not blocked, shorting_enabled, multiplier 4.
- **Reconciliation matches broker truth:** latest `reconciliation_log` rows `ok=t`,
  `broker={}`, `unexpected=[]` — DB book agrees with empty broker.
- **P&L truthful:** `pnl_daily` 2026-06-11 day P&L −$10.07, equity $100,027.22 == broker
  equity to the cent; 2026-06-12 row already opened flat (start_equity $100,027.22).
- **Kill-switch armed & NOT tripped:** `executor_state` 2026-06-12 `halted=f`; trips if equity
  < start − $150 (i.e. < $99,877.22), read from a FRESH `get_account()` each cycle; `halted`
  persists across restart and flatten runs regardless of it.
- **Caps bind from fresh broker snapshot:** gross cap = min(GROSS_CAP $6,000, equity×0.05 ≈
  $5,001) → binding = ~$5,001; a 6-name ~$200/name basket grosses ~$1.2k, well under.
- **Config confirmed:** DRY_RUN=false, MODE=paper, K=3/3, notional $200/name (intent = tiny).
- **Signal is tradeable, not degenerate:** last cadence scored 993 symbols, 343 distinct
  scores, spread 0.0157; top-3-long vs bottom-3-short separation 0.0140 ≫ MIN_SCORE_SEP
  0.0005 → the degeneracy guard passes on real spread, not tie-break noise. (Stale now;
  the 35-min staleness guard blocks trading until model-server writes fresh scores at the open.)
- **ETF defense-in-depth:** `candidate_pool` filters `is_etf_like(name)` + price ≥ $5 at
  selection, so even on the still-contaminated membership the basket excludes funds.
- **Termination proven yesterday & re-armed:** EOD flatten fired 15:48 ET → broker FLAT; the
  flatten path re-runs every cycle until flat (verify-by-retry) and also covers the
  market-closed `stranded` catch-up case.
**What the executor WILL do at the open:** idle (reject stale preds) until model-server writes
fresh scores within 35 min → submit ONE tiny NBBO marketable-limit 3L/3S basket → manage
(fills_log now records symbol/side; reconcile; pnl_daily) → TERMINATE at 15:48 ET.
**New this cycle:** realized-P&L attribution per name now works (`realized_pnl_by_name` view) and
reconciles to the cent (2026-06-11 per-name sum = −$10.07; worst MRVL short −$15.52, best LUNR
long +$4.68).
