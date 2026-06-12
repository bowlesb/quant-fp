# Execution & Alpaca API Reference + Design

Reference for executing our daily long/short cross-sectional basket (~40–60 small
orders/rebalance, 30min-to-overnight holds) on Alpaca — paper now, live later.
~$100k account, ~1000-symbol liquid universe, home server, second-scale latency,
post-PDT Intraday Margin Framework (2026).

## Verified hands-on (paper, 2026-06-10, market closed)

- Account ACTIVE, equity ~$100k, **multiplier 4** (daytrading BP ~$400k, RegT BP
  ~$200k), **shorting_enabled=True**, PDT=False.
- Limit + `extended_hours=True` while closed → `PENDING_NEW`→`NEW` (rests), cancellable.
- **Market order while closed → `ACCEPTED` (queued for next open), NOT rejected** —
  foot-gun; track/cancel by `client_order_id`.
- `cancel_orders()` returns the account to 0 open / 0 positions cleanly.
- Asset attrs carry `shortable / easy_to_borrow / fractionable / marginable`
  (mirrored daily into `asset_metadata`; short leg must filter on these).

## 0. TL;DR — the things that will bite us

1. **No simultaneous long + short of the same symbol** (Alpaca nets per symbol). A
   name that flips long→short across rebalances must have the long FULLY closed before
   the short accepts; open orders reserve shares too. Sequence flips explicitly. *The*
   structural constraint for a rotating cross-sectional book.
2. **Shorts only on ETB names**; HTB short opens are rejected. ETB↔HTB updates each
   morning — re-pull asset flags pre-rebalance; have a mid-basket fallback.
3. **Wash-trade protection within our one account → HTTP 403** if a buy & sell on the
   same symbol could cross. Risk during cancel-replace (old+new coexist) and
   entry/bracket-exit overlap. Bracket/OCO/trailing-stop are exempt.
4. **Bracket orders forbid extended hours** and require TIF day/gtc.
5. **Extended hours = limit only, TIF day/gtc, `extended_hours=true`** (no market, no
   stops, no auctions).
6. **Paper fills are optimistic** — qty NOT checked vs NBBO size; partials are random
   (~10%), not liquidity-driven. Don't infer live fill quality from paper.
7. **Open orders reserve buying power** until filled/canceled — a 50-order burst can
   exhaust BP and 403 later legs even if the net book is fine. Sequence + keep headroom.

## 1. Order types / classes / TIF

- **Types:** market (avoid for basket — no spread control, forbidden in ext-hours;
  ok only for forced near-close flatten), **limit (primary, as marketable-limit)**,
  stop / stop-limit (bracket safety leg; don't trigger outside RTH), trailing-stop
  (optional; RTH only; notional trailing can't be replaced). Limit price decimals:
  ≤2 if ≥$1.00, ≤4 if <$1.00.
- **Classes:** simple; **bracket** (entry+TP+SL, one fills other cancels; NO ext-hours,
  TIF day/gtc, TP>SL for a buy; both legs can fill in fast markets); oco (two exits,
  type must be limit); **oto (replacement NOT supported — matters since we
  cancel-replace)**; mleg (options, n/a).
- **TIF:** **day** = default for basket entries (RTH; only TIF for fractional/notional);
  gtc (resting overnight; auto-cancel 90d); opg (open auction; rejected after 9:28 ET);
  **cls** = MOC/LOC, **best for EOD flatten**, rejected after 3:50 ET; ioc
  (take-now-don't-rest); fok (all-or-nothing, rejects often on small names).
- **Recommended:** `day` marketable-limit for basket during RTH; `cls` LOC for EOD
  flatten; ext-hours `day` limits for deliberate overnight legs.

## 2. Shorting

- Only **ETB** shortable; flags (`shortable`,`easy_to_borrow`) refresh each morning —
  re-pull pre-rebalance, treat as necessary-not-sufficient (lender availability is
  real-time; can still reject). **ETB borrow fee = $0**; HTB charged on round lots.
- ETB→HTB while held: not force-liquidated unless recalled; you just pay borrow.
- **Non-shortable mid-basket handling (required):** (1) substitute next-ranked ETB
  short; (2) skip + re-normalize short weights; (3) reduce paired long to hold net.
  Log every substitution. Pre-filter on fresh flags so rejects are the rare tail.
- **Margin:** short maintenance — price<$5 → max($2.50/sh,100%); ≥$5 → max($5/sh,30%)
  → bias shorts to ≥$5. Short BP check reserves MAX(limit, ask+3%)×qty (conservative).
- **Post-PDT IML (live 2026-06-04):** PDT/$25k/day-trade-counting removed; real-time
  Intraday Margin Level; intraday P&L raises available margin immediately; 4x min
  equity now $2k. **API migration: `pattern_day_trader`, `daytrade_count`,
  `daytrading_buying_power` etc. are REMOVED by 2026-07-06 — do NOT gate logic on them**
  (our executor currently reads some of these; audit before live).

## 3. Lifecycle, streaming, reconciliation

- States: pending_new→new/accepted→(partially_filled)*→filled; terminal
  filled/canceled/expired/rejected. Can't cancel while `pending_replace`.
- **trade_updates websocket** (`wss://paper-api.alpaca.markets/stream`, auth then
  listen `trade_updates`): events new/fill/partial_fill/canceled/replaced/rejected/…;
  fill/partial_fill carry price, qty, and **signed `position_qty`** (authoritative
  running position — gold for reconciliation). alpaca-py: `TradingStream` (separate
  from our market-data `StockDataStream`).
- **`client_order_id` = idempotency key.** Make it deterministic from
  (rebalance_date, symbol, side, leg, attempt) and persist mapping BEFORE submit, so a
  retry after an ambiguous timeout re-submits the same id (dup is rejected → no
  double-place).
- **Reconcile from two sources:** stream-driven (primary, trust position_qty) +
  REST poll every N s and after any stream gap (`GET /orders?status=all` since cursor
  + `/positions`), diff vs target. **REST wins on reconnect** (stream can drop msgs).

## 4. Execution quality

- Ext-hours: limit only, day/gtc, wide spreads — deliberate use only.
- Auctions: **cls/LOC before 3:50 ET = clean EOD flatten**; opg before 9:28.
- Fractional/notional: TIF must be `day`; **notional can't be replaced** → use
  whole-share qty for anything we cancel-replace; keep shorts whole-share. Safest:
  **whole shares everywhere**.
- **Marketable-limit cross-by-≤1-tick:** buy=ask+1tick, sell/short=bid−1tick (tick
  $0.01 ≥$1). Pull fresh NBBO (we stream SIP) right before pricing each leg; recompute
  on each cancel-replace. `ioc` marketable = sweep; `day` marketable = rest remainder
  for the 30s loop.

## 5. Failure modes

| Failure | Code | Handling |
|---|---|---|
| Rate limit (~200/min/account; verify headers — may be higher) | 429 | token-bucket ~3-4/s; exp backoff + jitter; never tight-retry |
| Wash trade (opposing same-symbol could cross) | 403 | never have opposing open orders on a symbol; sequence flips |
| Insufficient BP/shares (open orders reserve) | 403 | pre-check BP, stage, headroom; re-sequence close-before-open |
| Bad params | 422 | validate client-side |
| Non-shortable | reject | pre-filter ETB; substitute/skip |
| Market-closed / wrong TIF for session | reject/queue | gate on `/clock`; route TIF per session |
| Long+short same symbol | reject | confirm close filled before reverse |

## 6. Paper vs live

Optimistic fills (qty unchecked; random partials), no slippage/impact/queue/borrow-fee
simulation; wash-trade protection IS active on paper (test it there); separate keys +
`paper-api` base URL; reset rotates keys (never hardcode). Validate fill quality &
BP behavior in a small live account before scaling.

## 7. L/S basket rebalance design

Pre-flight: `/clock` (pick TIF per session) → re-pull assets (longs tradable; shorts
shortable&etb, substitute non-ETB) → `/account` (BP/IML) + `/positions` → diff target
vs current into **closes → flips(after close confirmed) → opens**, each with a
deterministic `client_order_id`.
Submit: token-bucket ~3-4 orders/s; sequence closes→(await fills via stream)→flips→
opens to avoid wash-trade/long+short rejects; each leg fresh NBBO → marketable limit
(cross 1 tick) → day, ext-hours per session.
30s cancel-replace: re-price unfilled (whole-share only; cancel-then-new for notional);
cap re-prices (3-4) then escalate (widen, or LOC/IOC/market near close); never let an
opposing same-symbol order coexist; don't cancel while pending_replace.
Safety net: optional loose RTH-only brackets so a disconnect doesn't leave naked risk.
EOD flatten: LOC (`cls`) before 3:50 for intraday legs; market-close leftovers; cancel
stale working orders so they don't reserve BP overnight.
Reconcile continuously (stream + REST as above).

## 8. Stress-test matrix (paper first; * needs market open)

1 order types accept · 2 TIF routing (opg/cls cutoffs) · 3* marketable-limit fills ·
4* partial fills (stream qty; bracket SL auto-reduce) · 5* 30s cancel-replace race (no
double-fill) · 6* terminal-state cancel · 7 rate-limit backoff (burst) · 8 wash-trade
403 · 9* long+short flip sequencing · 10 non-shortable substitute/skip · 11*
insufficient-BP across 50 orders · 12 market-closed routing · 13* websocket reconnect /
REST recovery · 14* EOD LOC flatten · 15* idempotent retry (same client_order_id) ·
16* ext-hours limit entry.
Market-open scenarios run during paper RTH; but paper fills are optimistic, so #4/#11
must also be validated in a small live account before scaling.

## Key sources
Alpaca docs: orders-at-alpaca, margin-and-short-selling, user-protection (wash trades),
websocket-streaming (trade_updates), paper-trading, fractional-trading, end-of-PDT /
Intraday Margin Framework, common-trading-api-errors, usage-limit (rate). Verify before
live: exact rate limit (headers), HTB short status, short maintenance % under IML.

(Reference compiled from Alpaca docs + alpaca-py + hands-on paper probing, 2026-06-10.)

## Ledger — closed-loop verifications & fixes

### 2026-06-12 pre-open — closed-loop verify (all GREEN) + per-name P&L attribution shipped
Ran every check against FRESH broker truth (not asserted):
- Flat: 0 pos / 0 open orders; equity $100,027.22 == cash. Reconcile `ok=t`, `broker={}`.
- P&L truthful: `pnl_daily` 6/11 = −$10.07, equity == broker to the cent; 6/12 opened flat.
- Kill-switch armed, `halted=f` (trips < start−$150, fresh equity each cycle, persists across
  restart). Caps bind = min($6,000, equity×0.05 ≈ $5,001); ~$1.2k basket << cap.
- Signal non-degenerate: 993 syms / 343 distinct scores / L-S sep 0.0140 ≫ 0.0005 guard.
  Staleness guard (35min) blocks trading until fresh open scores land.
- **CLOSED the #1 open item — realized-P&L attribution per name.** Root cause: `fills_log`
  had no symbol/side, and EOD-flatten fills are broker-generated (not in `orders_log`) so they
  could not be joined back → per-name P&L was uncomputable. Fix: `capture_fills` now persists
  `order.symbol` + `order.side.value`; added cols (self-healing ALTER in executor startup +
  `db/init/01_schema.sql`) + `realized_pnl_by_name` view (signed cashflow per name/day).
  Backfilled 6/11 from Alpaca → per-name realized sums to −$10.07 EXACTLY (== pnl_daily):
  MRVL −15.52, PRIM −3.04, HUM −0.88, DXYZ +0.55, UMAC +4.14, LUNR +4.68. Validated end-to-end.
- Executor rebuilt+restarted clean (dry_run=false, paper); session state + reconcile resumed flat.
- TOP REMAINING HAZARD (flagged, not yet fixed): EOD flatten uses `close_all_positions` =
  MARKET orders at 15:48 ET. Works while open, but (a) no price control vs a LOC/`cls` net, and
  (b) the `stranded` market-closed catch-up path would submit market orders that QUEUE for the
  next open (ledger §0.foot-gun) rather than flattening now — only bites if a position ever
  lingers past close, which hasn't happened. Open items still: partial-basket cancel-replace,
  broker-side LOC EOD net.

### 2026-06-12 — per-leg execution slippage (MEASURED one-way cost) shipped
Built the measured cost curve the cost-gate battery only ASSUMED (cost_bps_oneway=2.0).
- Executor now persists the **arrival NBBO bid/ask/mid at submit** on orders_log (new cols);
  `marketable_limit` returns mid too. This is the only correct arrival benchmark — captured at
  the decision instant.
- Views (self-healing DDL + db/init/01_schema.sql): `execution_slippage` (per leg:
  `slippage_bps` = signed (fill − arrival_mid)/mid ×1e4, positive = cost paid; +`slippage_usd`;
  `arrival_src` ∈ {nbbo, bar_proxy}) and `execution_slippage_daily`
  (`oneway_cost_bps_mean/median` — the number to feed `long_short_backtest(cost_bps_oneway=)`).
- **HONEST CAVEAT (do not fool ourselves):** 6/11 legs predate the NBBO capture →
  `arrival_src='bar_proxy'` (bars_1m close of the last completed minute before submit). For the
  thin wide-spread names we actually trade (DXYZ, LUNR, UMAC), the minute-bar close is NOT a
  usable mid proxy — intra-minute noise (±50–125 bps) swamps the half-spread, producing
  nonsensical NEGATIVE "cost" (6/11 mean −41.7 bps is an ARTIFACT, NOT price improvement).
  Lesson: execution cost CANNOT be backfilled from minute bars; it must be captured live at the
  decision instant. The trustworthy one-way cost arrives 6/12+ from `arrival_src='nbbo'`.
- Modeller guidance: keep cost_bps_oneway=2.0 until several `nbbo`-sourced sessions accrue;
  then use `execution_slippage_daily.oneway_cost_bps_mean` (one-way, mid-referenced — directly
  comparable to the battery's breakeven_cost_bps). Round-trip ≈ 2× (entry + exit half-spread).

### 2026-06-12 OPEN — executor-half #6 verified live (all GREEN)
First live test of everything built last night, at the 09:30 ET open:
- **Stale→fresh transition WORKS:** executor idle-rejected day-old preds overnight (0 orders,
  staleness guard binding), then submitted ONE basket the cycle after model-server's first RTH
  cadence landed (preds ts 13:30Z, age ~3min < 35). Longs KEEL/SATS/UUUU, shorts AMPX/FLY/W.
- **Live NBBO arrival capture WORKS:** every order row has `nbbo_mid` populated; marketable limits
  correct (buys > mid, sells < mid). First `arrival_src='nbbo'` slippage row computed end-to-end
  (W sell: fill 80.00 vs arrival_mid 78.86). CAVEAT: n=1 filled leg → daily mean −144 bps is pure
  submit→fill DRIFT noise, NOT a cost signal (as forewarned; needs ~5–10 sessions to mean anything).
- **KLAC denylist BINDING in prod:** KLAC scored (rank 705, decile 9 = short candidate) but 0 KLAC
  orders for 6/12 — excluded from the actual basket. (KLAC stream now ~238 post-split vs ~2429
  yesterday — the 10:1 split took effect; denylist still HELD per the series-parity removal gate.)
- **Scores non-degenerate before submit:** 782 names, 260 distinct, L/S sep ~0.0134 ≫ 0.0005.
- **Reconcile = broker truth:** {W:-2, SATS:1}, unexpected=[], ok=t. Book building; partial fills
  (only 2 of 6 legs positioned so far — the open partial-basket item; rest rest until fill/EOD).
- Lifecycle now in MANAGE; EOD flatten will TERMINATE ~15:48 ET (verify post-close).

### 2026-06-12 — P1 #19: symmetric reconcile + spread-scaled cross + terminal status (WHY)
Triggered by QA probe `exec-recon-one-directional`: 6/12 basket intended 3L/3S, filled 2L/1S
(KEEL/FLY/AMPX never filled), yet reconcile reported ok:true all session — it was structurally
one-directional (only flagged UNEXPECTED broker positions). Three fixes:
1. **Spread-scaled marketable cross.** Root cause of the unfilled legs: a fixed $0.01 cross is
   non-marketable when the quote ticks on a wide-spread name between snapshot and submit (FLY's
   sell rested inside the bid). Fix: buffer = max(1¢, CROSS_SPREAD_FRAC×spread). ALTERNATIVES
   considered: (a) a bps-of-price term too — REJECTED, it over-crosses tight liquid names (10bps
   of $200 = $0.20 on a 2¢-spread stock); (b) cancel-replace loop — deferred (bigger change; the
   scaled cross is the cheap high-value fix now). Verified live: W/FLY get a proportional buffer,
   AAPL stays at 1¢.
2. **Terminal status writeback** (`sync_orders_and_fills` replaces `capture_fills`): one pass over
   today's orders writes current status + cumulative `filled_qty` to orders_log (was stuck at
   'submitted' → ledger showed all-time filled=0) and records fills incl. PARTIALS (a partial that
   later cancels was previously lost) using a STABLE terminal `fill_ts` so the upsert hits one row
   (no double-count). This is what makes reconcile able to see fill completeness.
3. **Symmetric reconcile**: compares intent vs broker BOTH ways. ok=false on unexpected positions
   OR rejected legs (real desync). partial/unfilled/orphaned-open + L/S basket-neutrality recorded
   in detail (not ok-breaking, so the EOD flatten's transient close orders don't flap ok) → feeds
   QA's per-day fill_reconciliation invariant (submitted == filled+accounted).
PROCESS NOTE: this Tier-1 diff was mechanically ABSORBED onto master inside prod's commit b856aa7
(a `git add -A` swept my uncommitted edits) BEFORE the new REVIEW_POLICY PR/review flow could run.
Code is on master but NOT deployed (running executor is the old image) → nothing un-reviewed is
live. Holding the deploy until the mapped reviewer (prod-architect for schema + QA for the
reconcile-invariant contract) signs off; escalated to Manager to regularize. Mission stakes: M4's
paper track record is meaningless if the basket we measure isn't the basket we decided.

## Active live-basket exclusions (remove when the condition clears — don't let these rot)
- **KLAC — excluded since 2026-06-12 (Manager pre-open directive).** Reason: KLAC's LIVE STREAM
  bars are persistently exactly 10× the true price (feed scaling bug, QA finding). The v1.1.x
  research panel is backfill-sourced and verified unaffected, but model-server computes live
  features from stream bars — most are scale-invariant ratios (a uniform 10× cancels), but a
  single non-10× bar spikes KLAC's returns and injects a garbage score into the cross-sectional
  rank, which could land it in the basket on artifact. At 3L/3S sizing exclusion costs ~0; KLAC
  was in fact ranked 957/993 (decile 9 = short candidate) on the latest scores. Mechanism:
  `SYMBOL_DENYLIST` in services/executor/main.py (filtered in `candidate_pool`; env-extensible
  via `SYMBOL_DENYLIST` for the Nx-sibling sweep). Verified live: pool 784 names, KLAC absent.
  **REMOVAL CONDITION:** prod-architect confirms the KLAC ingestion fix is LIVE *and* QA's parity
  check shows KLAC stream==backfill. Then drop KLAC from the denylist default. If the Nx-sibling
  sweep finds other symbols with Nx stream ratios, add them here with the same condition.
  NX-SWEEP RESULT (prod, 2026-06-12 pre-open): KLAC is the ONLY scale-anomalous symbol (median
  ratio 10.0000; no other symbol with ≥30 overlap bars deviates >10%; sub-1% canonical-close
  offsets deliberately don't qualify — they don't corrupt scores). => denylist is COMPLETE for the
  open; no additions needed.
  ROOT CAUSE (Alpaca Corporate Actions API, ground truth): NOT a feed bug — KLAC has a REAL 10:1
  forward split with ex_date = 2026-06-12. The "10×" was the split landing mid-backfill (pre-
  announcement months fetched raw, post-announcement adjusted) → stream and backfill were each
  internally right but the backfill SERIES is mixed-basis. EXEC NUANCE — do NOT pull the denylist
  on a superficial self-resolve: the live SPOT price realigns at the open (stream → post-split
  ~243), but the model's multi-bar features (mom_1d, gap_from_open, vwap_dev…) span the split
  boundary in the mixed-basis daily series, so KLAC's SCORE stays corrupt until prod's #17 series
  re-fetch on a consistent basis. Removal stays gated on QA parity (stream==backfill across the
  SERIES), not on the 09:30 spot looking right.

## Standing future items (open exec work, by gating milestone)
- **[BUILT 6/12, awaiting deploy] Automatic ex-date guard in candidate_pool (#18).** HAZARD CLASS
  (generalized from KLAC 6/12): any name with a split ex-date inside the feature-lookback window
  presents MIXED-BASIS features (live spot on the new basis, multi-bar lookback straddling the
  adjustment boundary) → garbage cross-sectional score → basket risk on artifact. IMPLEMENTED:
  `candidate_pool` now unions `SYMBOL_DENYLIST` with `ex_date_excluded(conn, today)`, which calls
  prod's `quantlib.corporate_actions.names_with_recent_ex_date(conn, as_of, EX_DATE_LOOKBACK_DAYS,
  SPLIT_TYPES)`. `EX_DATE_LOOKBACK_DAYS` default 15 (covers the longest live feature lookback,
  mom_10d ≈ 14 calendar days; env-tunable). Splits only by default (dividends don't corrupt
  intraday momentum). FAIL-OPEN until prod creates the `corporate_actions` table: the call catches
  `psycopg.errors.UndefinedTable` (verified SQLSTATE 42P01) → empty set + a loud warning, so the
  manual denylist still covers KLAC in the meantime. DEPLOY GATE: do NOT rebuild the executor until
  (a) post EOD-flatten (no live basket at risk) AND (b) prod's post-close CA fetch has created+
  populated the table — also the executor image must be rebuilt to pick up quantlib.corporate_actions
  (the running image predates it). Pairs with QA's price-match invariant (backstop for already-bad
  history). Two-layer defense, exactly as prod-architect-2 specified. Not blocking M1.
- **[M4/M5 — mandatory before real money] Settled-day reconciliation vs broker statements.**
  Paper has no statements to reconcile against, so this can't be exercised now — but the muscle
  (fills + realized P&L + borrow fees vs the broker's official daily statement, T+1 settled) is
  required before any real capital. Manager is adding it as an M4 exit criterion in ROADMAP.
  Owner: execution-risk. Build when a paper track record (M4) starts accumulating.
- **[post-M1, low priority] Stranded-position catch-up queues market orders for next open.**
  The market-closed `stranded` branch calls `close_all_positions` = MARKET orders, which Alpaca
  QUEUES for the next open rather than flattening now (ledger §0 foot-gun). Only bites if a
  position ever lingers past close (hasn't happened — EOD flatten fires at 15:48 while open).
  Resolution: route EOD termination through LOC/`cls` (broker-side net before 15:50 ET) so the
  flatten is auction-filled, and make the closed-market branch cancel-only rather than submit
  market orders. Pairs with the open "broker-side LOC EOD net" item.
- **[open] Partial-basket cancel-replace** — re-price unfilled legs (whole-share, no opposing
  same-symbol coexist) vs the 30s loop; cap re-prices then escalate near close.
- **[post-M1, pairs with LOC/cls EOD rework] Exit-leg cost capture → validate entry≈exit
  symmetry.** The Modeller's cost gate (long_short_backtest) is per-rebalance ONE-WAY: it charges
  cost_bps_oneway × Σ|Δweight|, so a full hold is already two one-way charges (entry 0→1/k, exit
  1/k→0). v1 correctly consumes the ENTRY-leg one-way slippage and applies it symmetrically. But
  that assumes exit cost ≈ entry cost — and our EOD-flatten exits are broker-generated MARKET-style
  fills with NO price control, vs marketable-limit entries, so exits may be systematically worse.
  Measuring exit cost is NOT a cheap view change: flatten fills aren't in orders_log and have no
  stored arrival mid, and the bar-close proxy is unusable on thin names (established finding). It
  requires capturing the NBBO mid per symbol AT FLATTEN time — do this when reworking EOD
  termination to LOC/`cls` (capture the auction/quote reference there). If exits prove materially
  worse, the Modeller moves to an asymmetric cost model (different bps on weight-decrease vs
  -increase). Not blocking M1; Modeller flagged it explicitly as a later validation.
