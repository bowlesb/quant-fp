# Cycle 2 synthesis — HF-liquid microstructure CLOSED; the structural wall + the forward brief

**2026-06-16.** Cycle 2 opened on menu #1 (HF-liquid intraday) — the one regime cycle 1 never tested, where
the capacity trap vanishes (megacap $10K orders are 0.015–0.06% of a minute's flow, 0.4–3 bps spread). Three
pre-registered hypotheses on the qimb (quote-imbalance) microstructure family, all KILLED.

## Cycle 2 scoreboard (0/3)

| H | bet | result | the killer |
|---|---|---|---|
| HF01 | qimb TAKER, directional, 30s–2m → next 1–5m mid-return | **KILL** | turnover-compounded cost: ~0.45 bps gross « ~2.7 bps round-trip |
| HF02 | qimb TAKER, LOW-turnover (15–30m hold + hysteresis) | **KILL** | per-trade bootstrap: net −0.20 bps/trade, CI straddles 0; the "+1.17 bps net" was an IC-weighted overlapping-return artifact |
| HF03 | qimb MAKER, liquidity-provision (earn the spread) | **KILL** | adverse selection (win rate 0.81→0.53) + no queue position; honest fill/exit → per-fill net ≤ 0; qimb fails the canary at +30/60s |

**The qimb microstructure signal is REAL** (canary-clearing, day-clustered-significant IC that grows with
horizon) **but it is neither TAKEABLE nor MAKEABLE at our latency/queue position:**
- Not takeable (HF01/HF02): the directional edge per trade is smaller than the spread you must cross.
- Not makeable (HF03): the earned half-spread is given back to informed flow (adverse selection), and any
  honest fill model (queue position) or honest exit (crossing to flatten) turns the per-fill net negative.
The qimb/microstructure family is **CLOSED**.

## THE META-NOTE — the SAME structural wall killed BOTH cycles

Cycle 1 (price reversion, order flow, 8-K/dividend/split events): 0/7 tradeable. Cycle 2 (HF-liquid micro):
0/3. **Every real signal we found is smaller than the friction required to harvest it** — just a different
friction each time:
- Cycle 1 illiquid signals (vwap_dev H1, 8-K drift H10, reverse-split H4): real, but the alpha lives in
  illiquid names where the SPREAD/IMPACT (30–800 bps) dwarfs it — and at Ben's $5–10K scale the illiquid
  ADV makes a $5K order 14% of volume (H13: capacity ceiling $0).
- Cycle 1 liquid + cycle 2 HF-liquid: liquid names are efficiently priced — the qimb micro-signal IS there
  but is smaller than the TAKER spread (HF01/02) or is eaten by ADVERSE SELECTION as a maker (HF03).
**One law: a signal must be LARGER than the friction at the liquidity tier where it lives. Every signal we
found fails it — illiquid signals to spread/impact/capacity, liquid signals to spread/adverse-selection.**
This is not a string of unlucky nulls; it is a coherent, repeatedly-confirmed structural finding about where
a small book can and cannot extract edge from PRICE/MICROSTRUCTURE data.

## What this rules out (do not re-tread)

- Daily/intraday cross-sectional PRICE signals (reversion, momentum, order-flow) — mapped, 0/dozens.
- Corporate-action-CALENDAR events (dividends, splits) on what we can trade — mapped.
- HF microstructure (quote-imbalance, OFI, signed-flow) as taker OR maker at our latency — mapped.
The remaining in-hand, zero-dependency directions are now EXHAUSTED.

## FORWARD BRIEF for Ben — the next cycle needs a DATA-ACQUISITION call

The honest read: we have wrung the price/microstructure/calendar data we own. A new edge almost certainly
requires a NEW INFORMATION SOURCE (content the price tape doesn't contain) OR genuine execution infra (real
queue-position/latency, which we don't have and is a hard, capital-intensive build). Ranked by EV-per-spend:

1. **#2 — EDGAR FUNDAMENTALS CONTENT (NO SPEND; we already own it).** We hold **3.2M point-in-time filings**
   (10-K/Q financials, 13F institutional holdings, 13D activist stakes) and have NEVER used the CONTENT —
   cycle 1 only used the 8-K event FLAG. The XBRL companyfacts API gives standardized fundamentals per CIK
   (one cheap call). The differentiated, less-crowded bets: **13D-activist-stake events on LIQUID targets**
   (a real information shock, not a calendar effect) and **13F institutional-holdings changes** — both gated
   on the liquid tier from the first test (the cycle-long lesson). This is the ONLY zero-spend new-information
   direction. **Top recommendation.**
2. **#3 — ETF / INDEX-REBALANCE FLOW.** Liquid-BY-CONSTRUCTION (index constituents) + mechanical price
   pressure (funds must trade the delta) — the rare class where the signal lives in tradeable names by
   design, the opposite of every illiquid trap. Needs the reconstitution CALENDAR (S&P/Russell add/drop
   dates — largely public, modest effort). Low-frequency but clean. **Strong second.**
3. **#4 — CROSS-ASSET (rates/FX → equity).** Free data (FRED). But low-EV: macro→equity timing is a
   beta-tilt more than alpha, heavily researched, low-frequency. A cheap side probe at best.
4. **#5 — OPTIONS-IMPLIED (vol surface/skew).** DEFER. No in-house data; an IV-surface vendor is expensive
   ($$$$) and a heavy new pipeline. Lowest feasibility-per-dollar; revisit only if #2/#3 are exhausted.

**Recommendation:** the next cycle starts with **#2 EDGAR-fundamentals-CONTENT** (zero spend, new information
we already own, liquid-gated) — most likely to break the "real signal < friction" wall because fundamental
re-pricing over days is a larger move than a microstructure wiggle, AND it lives in liquid names. Acquire
**#3's reconstitution calendar in parallel** (cheap, structurally clean). #4 a side probe; #5 deferred.

**This is a decision brief, not a hunt.** Per the standing rule, NO new hunt starts and NO data is acquired
(#2/#3/#5) until Ben picks the direction. The in-hand exploration is honestly complete: cycle 1 + cycle 2
mapped the price/microstructure/calendar edge space and found it empty at a tradeable scale. The platform,
the gates, the methodology toolkit (RESEARCH_PITFALLS, 7 entries) and the two live paper containers are the
durable assets; the next edge needs Ben's call on which new data to bring in.
