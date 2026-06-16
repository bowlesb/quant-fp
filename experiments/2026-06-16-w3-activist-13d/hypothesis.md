# W3 — 13D activist-stake events + 13F institutional-holdings drift on LIQUID targets (pre-registration)

**Registered:** 2026-06-16 BEFORE running. Lens L5 (EDGAR fundamentals CONTENT — never mined; cycle-1 used
only the 8-K event flag). Friction-wall design: a LARGE, SLOW INFORMATION SHOCK (an activist takes a >5%
stake / files 13D; institutions rotate holdings) re-priced over WEEKS — low turnover, and on LIQUID targets
(13D/13F targets are mostly real, listed companies). This is the most DIFFERENTIATED bet in wave 1: it's a
genuine information event the price tape does not contain, and it's far less crowded than price factors.

## Hypotheses (two sub-tests)
1. **13D activist drift:** when an activist files a Schedule 13D (>5% stake with intent to influence) on a
   LIQUID target, the stock drifts UP over the following days/weeks (the documented activist-announcement
   premium — Brav-Jiang-Partnoy-Thomas). 13D/A amendments (stake changes) too.
2. **13F holdings-change drift:** quarter-over-quarter increases in institutional ownership (aggregated 13F
   holdings) predict positive forward drift on LIQUID names (the institutional-demand / smart-money effect).

## Universe + data
- `filings` table (3.2M PIT, available_at look-ahead-safe). 13D: form_type 'SC 13D' (+ 'SC 13D/A'); we have
  7,130 SC 13D + their amendments. 13F: 'SC 13G' (passive >5%) for contrast + the 13F-HR (3,517) for the
  institutional-aggregate (note: 13F-HR is the FILER's whole portfolio; mapping to per-TARGET holdings needs
  parsing the 13F XML information table — scope a sample if slow, flag the parse).
- Entry = D+1 OPEN after available_at (tradeable, UTC-correct). Forward returns {1,3,5,10,20,40,60} trading
  days (activist drift is slow — go to a quarter). LIQUID tertile (the H10b lesson — PRIMARY gate).

## Test design
- **13D cohort:** event = SC 13D / SC 13D/A available_at on a liquid-tier target. Cohort forward drift vs
  same-date non-event controls; equal-weight long the 13D-target cohort (the documented direction is
  positive). Per-symbol demean; canary; OOS; per-trade bootstrap on D+1->D+H realized round-trips; cost gate.
- **13F sub-test (if the XML parse is tractable on a sample):** sign liquid names by QoQ change in aggregate
  institutional holdings; cross-sectional L/S; same gates. If the parse is too heavy, defer 13F to a follow-up
  and report 13D alone (13D is the cleaner, higher-signal event).
- DECISIVE: LIQUID-tier OOS cohort/L-S net-of-cost drift, per-trade bootstrap CI > 0.

## Expected / confidence
- Confidence the LIQUID 13D drift clears net-of-cost OOS with bootstrap CI > 0: **~35%** — among the highest
  priors in the program because (a) the activist-announcement premium is one of the better-documented,
  less-crowded EVENT effects, (b) it's a large, slow, multi-day move (very friction-favorable), (c) it lives
  in real listed targets (not micro-cap-only), and (d) it's genuinely new information (not in the price
  tape). Risks: 13D events are RARE (the liquid-tier count over ~6 months may be THIN — pre-commit: if the
  liquid 13D count < ~30, report "directionally suggestive, needs deeper history", not a confident verdict),
  and the announcement pop may already be priced by D+1-open entry (test entry timing).
- KEEP-AS-LEAD: liquid 13D cohort OOS net positive, bootstrap CI > 0, adequate N, demean+canary survived ->
  an event-driven paper container + the 13F follow-up. AMBIGUOUS / "needs history": thin N. KILL: no drift
  beyond canary OR net <= 0 with adequate N.

## Friction-wall scorecard
[info-shock ✓ activist event — NEW information, not price] [low-turnover ✓ multi-week drift] [liquid ✓
real targets, liquid-gated] [less-crowded ✓✓ vs price factors] — the most differentiated wave-1 bet. The main
risk is sample size (13D events are rare), not the friction wall.

## Data-ask flag
If 13D liquid-tier N is too thin over 6 months, the deeper EDGAR history (we have filings back to 1994 — but
bars only 126d) suggests the ask: extend BARS history to match the filings depth, enabling a multi-year 13D
event study. Flag to the Lead if W3 is promising-but-thin.
