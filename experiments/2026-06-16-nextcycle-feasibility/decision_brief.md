# Next-cycle feasibility audit — a decision brief for Ben (pre-registration-grade scoping only)

**2026-06-16. Scoping ONLY — no capital, no feature groups, no fingerprint changes.** The cycle-1 hunt
mapped the price + corporate-action-calendar classes and found 0 tradeable edges, with one unifying
constraint: *every canary-clearing signal was illiquid-concentrated, and illiquidity is what made it both
detectable (slow price discovery) and untradeable (you move the price)* — confirmed dead even at Ben's $5–10K
scale (H13: illiquid median ADV $35.8K → a $5K order is 14% of volume, capacity ceiling $0). This brief
ranks the 5 candidate *liquid-carrying* signal classes against our ACTUAL data/infra, with the **H13
capacity-ceiling lens applied first** — a class only matters if its signal lives in names liquid enough to
trade at $100K with positive capacity.

## The capacity baseline that reframes everything (measured)

The H13 trap is a property of ILLIQUID names, and it VANISHES in liquid ones (measured, 2026-06-12 RTH):

| name | median $vol / MINUTE | a $10K order = | half-spread |
|---|---:|---:|---:|
| SPY | $66.2M | 0.015% of one minute | 0.40 bps |
| TSLA | $48.5M | 0.021% | 3.0 bps |
| NVDA | $37.7M | 0.027% | 1.5 bps |
| MSFT | $18.4M | 0.054% | 1.6 bps |
| AAPL | $16.7M | 0.060% | 0.5 bps |

**At $100K across ~10 megacaps, capacity is effectively unbounded and per-trade cost is ~0.5–3 bps.** The
binding constraint flips from cycle 1: liquid names have ALL the capacity and almost no cost — the open
question is whether they carry any *signal* a real-time system can exploit faster than they're arbitraged.
(Caveat: top-of-BOOK snapshot depth is thin even on AAPL — ~$58K — so a single marketable $10K order pays a
few bps of impact; but spread over a minute / worked passively it's negligible. The relevant capacity metric
is per-minute flow, not the top-of-book snapshot.)

---

## RANKED feasibility (highest EV-per-effort first)

### #1 — HF-LIQUID intraday (megacap real-time) — **DATA & INFRA MOSTLY HERE; the natural next cycle**

- **DATA WE HAVE:** raw trades for all 7,668 names (21–126 days; megacaps 63–126d) + quotes for the
  top-2,504 (21–63d) in `/store/raw`. The per-minute tick-aggregation primitives (`quantlib/aggregates.py`,
  parity-true), the feature bus (`quantlib/bus/` — publisher/consumer/codec/schema, live fingerprint
  0xcc8f2aef…), and TWO working strategy containers (smoke + reversion, paper, live) already exist. The
  bet→vector latency work is partly built.
- **DATA WE'D NEED:** essentially none new for research — we have the tape. For a LIVE intraday strategy:
  the real-time quote/trade subscription at the chosen-symbol scope (the platform streams a subset today);
  extending to a clean N-megacap real-time book is plumbing, not acquisition.
- **MINIMUM NEW PLUMBING:** a real-time intraday signal computed from the live vector (the bus already
  delivers per-minute vectors), and a strategy container that acts within the minute (the reversion container
  is the template; it already consumes the bus + places paper bets with caps). No new data pipeline.
- **COST REALITY:** 0.5–3 bps/side on megacaps (measured). A signal needs only to clear ~1–6 bps round-trip —
  10× easier than the illiquid 30–800 bps wall that killed cycle 1.
- **CAPACITY (H13 lens):** UNBOUNDED for $100K — a $10K order is 0.02–0.06% of one minute's flow. The trap
  does not apply.
- **THE REAL RISK (honest):** megacaps are the MOST efficiently-priced names — cycle 1 found the liquid tier
  has ~no cross-sectional signal at the minute/hour scale. HF's bet is that a *real-time, within-minute,
  microstructure* signal (order-flow imbalance on the live book, queue dynamics, sub-minute momentum,
  liquidity-provision/rebate capture) exists at a horizon SHORTER than we tested cross-sectionally. That is
  genuinely untested in our stack and is where low cost + high capacity could finally let a small signal pay.
- **FEASIBILITY / EV:** HIGH feasibility (data + infra in hand), MEDIUM-HIGH EV (efficient names = hard, but
  the cost/capacity math is finally favorable and the horizon is unexplored). **The lowest-regret next cycle:
  it reuses everything built and directly tests the one regime cycle 1 didn't.**

### #2 — GENUINE FUNDAMENTALS from the EDGAR corpus — **DATA IS HERE (huge); parsing is the cost**

- **DATA WE HAVE:** the `filings` table is now **3.17M filings, 5,328 symbols, 1994→2026** — incl. **121K
  10-Qs, 41K 10-Ks, 47K 13D/13G (activist/large-holder), 3,517 13F-HR (institutional holdings), 1.36M
  Form-4s** — all with look-ahead-safe `available_at`. This is a deep, point-in-time fundamentals/ownership
  substrate we have NOT mined (cycle 1 only used the 8-K *event flag*, not the *content*).
- **DATA WE'D NEED:** none new — but the financials are inside the filing documents (XBRL financial
  statements in the 10-K/Q; holdings in the 13F). Need a PARSER: SEC provides structured XBRL "company facts"
  (`data.sec.gov/api/xbrl/companyfacts/CIK…json`) giving standardized fundamentals (revenue, EPS, margins,
  accruals) per company per period — no document scraping. 13F holdings parse from the filing XML.
- **THE LIQUID-CARRYING QUESTION (H13 lens UP FRONT):** classic fundamental anomalies (accruals, quality,
  value, 13F-cloning, activist-13D drift) are known to be STRONGER in small caps (the cycle-1 trap risk).
  BUT some are documented in large caps (13F institutional-holdings changes, 13D activist events on liquid
  targets, earnings-quality on the S&P). The audit-up-front rule: only pursue the fundamentals that have a
  documented LIQUID-tier effect, and gate on the liquid tertile from the first test (the H10/H5 lesson).
- **FEASIBILITY / EV:** MEDIUM feasibility (the XBRL companyfacts API removes the parsing bottleneck — one
  call per CIK, like the 8-K items), MEDIUM EV (rich orthogonal data, but the liquid-tier-only constraint and
  the heavy-arbitrage of known factors temper it). A 13F-change / 13D-activist study on LIQUID targets is the
  most differentiated sub-bet (event + ownership, less crowded than accruals).

### #3 — ETF / INDEX-REBALANCE FLOW — **PARTIALLY buildable; the calendar is the gap**

- **DATA WE HAVE:** the price/volume tape (we'd SEE the rebalance-day volume spike), and EDGAR (some fund
  filings). Index-reconstitution events (S&P, Russell) are PUBLIC scheduled events with known effective dates.
- **DATA WE'D NEED:** the index-membership-change CALENDAR (which names added/dropped from S&P 500 / Russell
  on which date) — NOT in our store. Sources: public (S&P/Russell announce reconstitutions; Russell annual
  June reconstitution is a known large-flow event), or a vendor. The membership deltas are the event list.
- **LIQUID-CARRYING (H13 lens):** GOOD — index additions/deletions are BY DEFINITION liquid names (S&P/Russell
  constituents), and the rebalance-flow effect (index funds must trade the delta) is a documented,
  mechanical, LIQUID price-pressure event. This is the rare class where the signal lives in tradeable names by
  construction — the OPPOSITE of cycle 1's illiquid trap.
- **FEASIBILITY / EV:** MEDIUM feasibility (need to build/acquire the reconstitution calendar — moderate
  effort, mostly public), MEDIUM-HIGH EV *conditional on the calendar* — it's liquid-by-construction and
  mechanical, which is exactly what cycle 1 lacked. Sample size is small (a few reconstitutions/year) so it's
  low-frequency, but low-turnover + liquid + mechanical is a clean shape. **The most STRUCTURALLY promising
  non-HF class** if the calendar is acquirable.

### #4 — CROSS-ASSET (rates / FX → equity) — **NO data; cheap to acquire; lower differentiation**

- **DATA WE HAVE:** nothing — Alpaca (our only feed) is equities/crypto, no rates or FX.
- **DATA WE'D NEED:** rates (FRED — free: Treasury yields, the 2s10s, real yields) and FX (cheap/free).
  Acquisition is EASY and free for daily; intraday FX/rates needs a feed.
- **LIQUID-CARRYING (H13 lens):** the signal would be a MACRO factor applied to LIQUID equity baskets
  (rate-sensitive sectors, USD-exposed names) — liquid by construction, capacity fine.
- **FEASIBILITY / EV:** HIGH feasibility (free data), but LOW-MEDIUM EV — cross-asset macro→equity timing is
  heavily researched, low-frequency, and a weak per-trade signal; it's a beta-tilt more than an alpha. Worth a
  cheap probe but not a primary bet.

### #5 — OPTIONS-IMPLIED (vol surface / skew) — **NO data; EXPENSIVE; defer**

- **DATA WE HAVE:** nothing — no options data source.
- **DATA WE'D NEED:** an options chain / implied-vol feed (ORATS, OptionMetrics, CBOE, or Polygon options).
  Historical IV-surface data is EXPENSIVE (the good vendors are $$$$) and the parity/storage burden is large.
- **LIQUID-CARRYING (H13 lens):** options signals (put/call skew, IV-rank, gamma exposure) are about LIQUID
  underlyings, so capacity is fine — but the DATA acquisition cost is the blocker.
- **FEASIBILITY / EV:** LOW feasibility (expensive data, new infra), unknown EV (real but heavily-mined
  effects like vol-risk-premium / skew-predicts-returns). **DEFER** — highest data cost, no in-house data,
  lowest feasibility-per-dollar. Revisit only if HF + fundamentals + ETF-flow are exhausted.

---

## THE RANKED MENU (for Ben's pick)

| # | class | data we have | data we'd need | feasibility | EV | liquid/capacity |
|---|---|---|---|---|---|---|
| 1 | **HF-liquid intraday** | trades+quotes + bus + containers (ALL) | ~none (plumbing only) | **HIGH** | MED-HIGH | UNBOUNDED ✓ |
| 2 | **EDGAR fundamentals** (13F/13D/XBRL) | 3.17M filings PIT (HUGE) | XBRL companyfacts parser (cheap API) | MED | MED | liquid-IF-gated ✓* |
| 3 | **ETF/index-rebalance flow** | price tape; EDGAR | reconstitution calendar (public) | MED | MED-HIGH | liquid-by-construction ✓✓ |
| 4 | cross-asset (rates/FX) | none | FRED rates + FX (free) | HIGH | LOW-MED | liquid ✓ |
| 5 | options-implied | none | IV-surface vendor ($$$$) | LOW | ? | liquid ✓ (data is the wall) |

**My recommendation (one line):** start cycle 2 with **#1 HF-liquid intraday** (lowest regret — reuses all
built infra, directly tests the unexplored short-horizon regime where our finally-favorable cost/capacity
math can let a small real-time signal pay), and **acquire the #3 ETF-reconstitution calendar in parallel**
(the most structurally promising liquid-by-construction event class, cheap to source). #2 fundamentals is the
strong third (deep PIT data already in hand; gate on liquid tier from the first test). #4 is a cheap side
probe; #5 defers on data cost.

**What this brief is NOT:** it is not a hypothesis or a result — it is the menu. Each chosen class then gets
its own pre-registered hypothesis (idea/prior/test/cost+capacity gate/kill) before any run, per the charter.
Cycle 2 starts the instant Ben picks a row.
