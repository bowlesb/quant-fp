# Corporate Actions feed + adjustment-consistency (tasks #18 + #17)

Owner: prod-architect (data acquisition lane). Status: CORE BUILT 2026-06-12 (table DDL + fetch/
upsert module + backfiller tool), wiring + first fetch pending (see "Build status" below). This note
captures the validated API shape, the KLAC root cause, and the fix design.

## Build status (2026-06-12)
DONE (staged, import-validated, no live-service change yet):
- `db/init/05_corporate_actions.sql` — table DDL (PK symbol/action_type/ex_date, ex_date index).
- `quantlib/corporate_actions.py` — `fetch_corporate_actions()` (per-type pydantic parse, guarded),
  `upsert_corporate_actions()` (idempotent; RETURNS the set of symbols with a NEWLY-inserted action
  = the #17 re-fetch trigger), `names_with_recent_ex_date()` (executor ex-date guard consumer).
- `services/backfiller/main.py fetch-corporate-actions` — tool subcommand (CA_START/CA_END/
  BACKFILL_SYMBOLS env; logs new-action symbols).
PENDING (DB-write / restart — batched post-close, or on Manager's DB-clear):
1. Apply DDL to the live DB + first fetch over the universe (`fetch-corporate-actions`).
2. backfill-manager: on `upsert` returning new-action symbols, full-history single-pass re-fetch
   of each (closes the #17 mid-backfill class permanently).
3. scheduler: daily rolling CA fetch (last 30d + next 35d) so the feed stays current.
4. Consumers in other lanes: QA wires the table into the jump invariant; execution wires
   `names_with_recent_ex_date()` into the candidate-pool guard. Needs a QA unit test for the parser.

## Why this exists
The KLAC incident (2026-06-12): a real **10:1 forward split, ex-date 2026-06-12** (announced
~6/4) landed DURING KLAC's incremental backfill. months fetched pre-announcement were raw, months
fetched post-announcement were split-adjusted (Adjustment.ALL) → a 10× discontinuity inside one
symbol's bar series → corrupted momentum features + a garbage live score (mixed-basis). Without an
authoritative corporate-action source we can only stumble onto this; with one we self-gate.

## Validated API (alpaca-py)
```python
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data.requests import CorporateActionsRequest
c = CorporateActionsClient(KEY, SECRET)
res = c.get_corporate_actions(CorporateActionsRequest(
        symbols=[...], start=date(...), end=date(...)))   # types= optional filter
res.data  # dict keyed by action type
```
- **Account already entitled** (it's our broker — no new vendor/cost). Confirmed working.
- `res.data` keys: `forward_splits`, `reverse_splits`, `cash_dividends`, `stock_mergers`,
  `cash_mergers`, `name_changes`, `unit_splits`.
- Items are **pydantic objects (NOT dicts)** — use attribute access, not `.get()`. Fields seen:
  - splits: `symbol, ex_date, old_rate, new_rate, record_date, process_date, payable_date`.
  - dividends: `symbol, ex_date, rate, record_date, special`.
  - (some types e.g. stock_mergers use different field names — guard per-type before access.)
- Spot-check result (11 flagged names, 2023-12..2026-06): KLAC 1→10 (ex 6/12); reverse splits
  BMNR 20:1, INHD 10:1/24:1/20:1, QXO 8:1, STI 50:1 — all the reverse splits were announced/
  effective BEFORE those symbols were backfilled, so they were consistently adjusted (no
  discontinuity). KLAC's was the only one landing mid-backfill.

## #18 build (post-M1 / M2)
1. **Table** `corporate_actions(symbol, action_type, ex_date, old_rate, new_rate, cash_rate,
   record_date, payable_date, raw jsonb, ingested_at)` — PK (symbol, action_type, ex_date).
2. **Fetcher** in `scheduler` (daily job) or a small tool: pull a rolling window (e.g. last 30d +
   next 30d) for the universe, upsert. Cheap, low-volume.
3. **Consumers:**
   - QA invariant (their lane): primary artifact detector = **"latest backfill close ≈ a fresh
     Alpaca quote within ~5%"** (this UNIQUELY caught KLAC and cleared all 10 real movers). The
     >3×-day-jump probe is secondary and MUST cross-check the CA table — note KLAC's discontinuity
     is at the fetch-timing boundary (6/01), ~11 days off its ex-date (6/12), so ex-date matching
     alone is insufficient; the price-match invariant is the reliable one.
   - Overnight-label dividend handling (future): the split-only basis can become split+div-aware.

## #17 adjustment-consistency fix (prod-architect)
1. **One-shot re-fetch KLAC** full history in a single Adjustment.ALL pass (post-split, post-close)
   → continuous series. Then recompute KLAC's v1.1.1 momentum cells (small) and clear the panel
   caveat for KLAC. Re-evaluate the live denylist (likely self-heals post-open once the split takes
   effect and stream drops to the post-split scale matching the adjusted backfill — VERIFY post-open).
2. **Standing guarantee in backfill-manager:** when the CA feed reports a NEW action for a symbol,
   re-fetch that symbol's WHOLE history in one pass (don't patch months). Rare event, one symbol at
   a time — cheap. This closes the "split announced mid-backfill" class permanently.
3. **Live path:** ensure the model-server's live momentum uses daily_closes on the SAME adjustment
   basis as the intraday stream bars (the KLAC garbage score came from mixing raw-stream intraday
   with adjusted-backfill daily). Either adjust live daily_closes to raw, or stream to adjusted —
   pick one basis and make it consistent end-to-end.

## Sequencing
Post-M1 critical path + post-today's-close (DB-heavy re-fetch; ingestor restart batched post-close
to protect QA's #15 full-session day). The KLAC re-fetch (#17 step 1) is the only near-term item.
