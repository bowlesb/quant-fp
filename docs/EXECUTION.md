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
