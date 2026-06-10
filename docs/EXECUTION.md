# Execution & Alpaca API — working notes

The execution track was under-attended (we over-focused on the data/modeling
pipeline). This is the living reference; a deep research report is being folded in.
Goal: master the Alpaca trading API, stress-test it, and run trivial paper trades
NOW to exercise signal→order→fill→reconcile end-to-end — without waiting for the
full dataset.

## Verified hands-on (paper account, 2026-06-10, market closed)

- **Account:** ACTIVE, equity ~$100k, **multiplier 4** (daytrading BP ~$400k, RegT BP
  ~$200k), **shorting_enabled=True**, pattern_day_trader=False, not blocked. So the
  paper account supports the long/short margin book we need.
- **Order behavior:**
  - Limit order with `extended_hours=True` while closed → `PENDING_NEW` → `NEW`
    (resting), cancellable. Good for pre/post and for resting marketable limits.
  - **Market order while closed → `ACCEPTED` (QUEUED for next open), NOT rejected.**
    Foot-gun: a stray market order placed after close will fire at the next open.
    Always cancel/track by `client_order_id`.
  - `cancel_orders()` cancels all open; verified account returns to 0 open / 0 pos.
- **Asset attrs** carry `shortable / easy_to_borrow / fractionable / marginable`
  (AAPL/SPY/NVDA all true). Already mirrored daily into `asset_metadata`; the short
  leg must filter on these.

## Open questions / to verify (stress-test matrix — * = needs market open)

- Order types: market, limit, stop, stop-limit, trailing-stop; bracket/OCO/OTO; TIF
  (day/gtc/opg/cls/ioc/fok). Which fit a 30m-to-overnight basket. *
- Partial fills behavior + trade-updates websocket stream; cancel/replace races. *
- Shortability failure mid-basket (name not shortable / HTB) → error handling.
- Wash-trade rejection (long + short same name across the book).
- Rate limits (orders/min) + 429 backoff when firing ~40-60 basket orders.
- Fractional shares constraints (with shorting / limit / extended hours).
- Market-closed vs open handling; opening/closing auction (opg/cls). *
- Paper-vs-live fill optimism; reconciliation robustness.

## Planned execution work (parallel to the modeling pipeline)

1. **Trivial paper strategy NOW** (upgraded later): a tiny scheduled rotation/basket
   (e.g. a few liquid longs + a few shortable shorts, marketable limits, EOD flatten)
   to exercise the full path and stress execution — independent of the ML panel.
2. **Execution stress tests** against paper (the matrix above), automated where
   possible; market-open scenarios scheduled for RTH.
3. **L/S basket rebalancer design:** submit/track/cancel-replace ~40-60 orders within
   rate limits; marketable limit + 30s cancel/replace; server-side bracket exits as a
   safety net; EOD flatten; robust reconciliation by client_order_id.

(Deep API reference from the research agent will be merged here.)
