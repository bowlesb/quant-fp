# MULTI-DAY (WEEKLY) HORIZON — cross-sectional factor study (PRE-REGISTRATION + DESIGN)

**Author:** Modeller · **Date:** 2026-06-19 (PT) · **Status:** PRE-REGISTERED DESIGN — written BEFORE any
outcome. This cycle the deliverable is the DESIGN + pre-registration of a genuinely-different surface; the
full run can wait for the deeper quote tape (DataIntegrity is backfilling now) if a leg needs it. A clean
design of a fresh surface IS the deliverable (per the Lead).

## WHY THIS AVENUE (rationale — the framing pivot)

We have **5 settled DIRECTION-nulls** (price ×2 framings, order-flow, EDGAR+sector #187, 8-K event #197)
and a twice-confirmed "intensity is real but own-vol-explained, not alpha" picture. EVERY one of those
hunts is **intraday/overnight** with **high turnover** — and the recurring kill was COST: the intensity
signals were tail-driven and net-negative once a realistic round-trip was charged (the #197 absret_abn
"prize" died exactly there). The **multi-day / weekly horizon is genuinely untested** and structurally
attacks that failure mode: **lower turnover → cost is amortized over a multi-day holding period → a signal
an order of magnitude weaker per-period can be net-positive.** It is the Lead's lead recommendation and the
one avenue that is both fresh AND cost-favorable. (NON-prediction/portfolio and cross-asset are the
fallbacks; horizon is chosen because the deep daily panel exists NOW and the cost logic is the cleanest
fix for the exact thing that killed the last 5 hunts.)

## ⚠️ THE CENTERPIECE CONSTRAINT — SURVIVORSHIP (verified, non-negotiable)

The deep `fp_store_real` raw-bar panel (2016→2026, 7,703 symbols, minute bars aggregating cleanly to daily)
is **fully survivorship-biased**: I verified **0 of the first 400 symbols have bars ending before 2025** —
the store holds only currently-listed names, every one carried back to 2016. `universe_membership` (the
survivorship-aware universe table) only goes back **2026-06-15** (the live universe), so a HISTORICAL
multi-day universe must be reconstructed from the bars themselves = survivorship-biased by construction.
This is the EXACT trap that downgraded the Lane-C/B4 overnight finding ([[project-b4-overnight-survivor]],
[[reference-overnight-data-traps]]). So survivorship is not a footnote — it is the design's spine:

- **Pick a hypothesis where survivorship works AGAINST or is small for the edge**, not for it. A multi-day
  REVERSAL long-the-losers / short-the-winners bet is the right choice: survivorship bias INFLATES a
  loser-buying strategy (the delisted losers that would have hurt it are ABSENT), so survivorship is a
  KNOWN HEADWIND-removed → any edge I find is an UPPER BOUND, and I pre-commit to reporting it as such
  and stress-testing it with a delisting haircut. (The opposite — a momentum/winner-buying bet — would be
  survivorship-FLATTERED in the dangerous direction; avoided.)
- **Restrict to a LIQUID large-cap universe** (top-N by trailing ADV dollar), where abrupt delisting is
  rarest, so the residual survivorship bias is smallest and most quantifiable.
- **Pre-committed delisting stress test:** re-run assuming every name that DISAPPEARS from the panel
  (stops printing bars) realized a −30% / −100% return over its final holding period (the standard
  delisting-return haircut). If the edge survives the −100% haircut it is survivorship-robust; if it
  vanishes, it was survivorship — reported honestly as such.

## SUBSTRATE (verified)
- Deep minute bars `fp_store_real` 2016→2026, daily-aggregatable (RTH last-close + dollar-volume; ET-anchored,
  Int32-cast to avoid the #197 Int8-overflow bug). 2,630 trading days for SPY.
- $1-floor + per-week symmetric winsorization mandatory (the overnight/multi-day bad-print trap,
  [[reference-overnight-data-traps]]).

## HYPOTHESES (pre-registered — 2, both LOW-turnover, cost-favorable)

### H1 — WEEKLY SHORT-TERM REVERSAL (primary)
**Claim:** the cross-sectional 1-week return reverses over the NEXT week — buy last week's losers / short
last week's winners, rebalanced weekly. The most robust documented cross-sectional anomaly; weekly turnover
(~1 trade/name/week) makes cost ~5–10× less binding than the intraday hunts.
- **Feature:** `rev_1w` = the name's trailing 5-trading-day return, as of each weekly rebalance (Friday
  close, entered the FOLLOWING Monday at a tradeable price ≥09:35 ET — never the close-to-close look-ahead).
- **Target:** the forward 5-trading-day return (next week), entered at the tradeable Monday open+5min.
- **Test:** weekly cross-sectional rank-IC of `−rev_1w` vs forward weekly return (reversal = negative IC of
  the raw return → positive IC of its negative), NW-t over the weekly IC series; AND the decile L/S spread
  NET of cost.
- **Predicted:** a small POSITIVE reversal IC that — critically — must survive (a) the own-vol/size control
  (is it just illiquid small-caps reverting? partial out trailing vol + size), (b) net-of-cost at WEEKLY
  turnover, (c) the survivorship delisting haircut. Honest prior: reversal is real in academ­ic data but
  thins in liquid names + after cost; the open question is whether the lower-turnover horizon lets it clear
  cost where the intraday signals could not.

### H2 — WEEKLY LOW-VOLATILITY / BETTING-AGAINST-BETA (secondary)
**Claim:** low-realized-vol names earn higher risk-adjusted forward weekly returns than high-vol names (the
low-vol anomaly) — a low-turnover (vol is persistent → slow rebalance) factor.
- **Feature:** `vol_20d` = trailing 20-trading-day realized vol as of the rebalance.
- **Target:** forward weekly return (and forward weekly Sharpe-like = return/own-vol).
- **Test:** rank-IC of `−vol_20d` vs forward weekly RISK-ADJUSTED return; decile L/S net-of-cost; the
  delisting haircut.
- **Predicted:** low-vol is a slow factor → survivorship matters LESS (low-vol names rarely delist
  abruptly) → the cleanest survivorship case. Open question is net-of-cost magnitude + whether it is
  distinct from a pure size tilt.

## DISCIPLINE (non-negotiable — same spine as #187/#197, plus the survivorship leg)
- **Tradeable entry:** weekly rebalance entered at the Monday (next session) open **≥09:35 ET**, never the
  Friday close (no close-to-close look-ahead); forward label from that tradeable entry.
- **Liquid universe:** top-N by trailing 20d ADV dollar, reconstructed point-in-time from the bars
  (the only available historical universe), with the survivorship caveat stated.
- **Shuffle baseline** (permute the forward label within each weekly cross-section, ≥200 iters) + **predict-
  zero** baseline.
- **Own-vol / SIZE control** (the #187/#197 lesson): partial out trailing realized vol + a size proxy
  (log dollar-volume) from both sides; report the collapse ratio. A reversal that is just "small illiquid
  names bounce" must be flagged.
- **OOS:** walk-forward by year (2016-2021 fit / 2022-2026 confirm) + report per-year IC sign-consistency.
- **NET-OF-COST:** the decile L/S spread net of 5/10 bps round-trip at WEEKLY turnover (the whole point);
  report the median + mean + the break-even cost.
- **$1-floor + per-week symmetric winsorization**; label-std sanity check (the bad-print/overnight guard).
- **SURVIVORSHIP delisting haircut** (the centerpiece): −30% and −100% terminal-return stress on
  disappearing names; report the edge under each.
- **BY-FDR** (q=0.10) across all (hypothesis × control-variant × horizon) cells.

## STOP CONDITIONS (pre-committed)
- A reversal/low-vol edge that survives shuffle + own-vol/size control + net-of-cost AT WEEKLY TURNOVER +
  the −100% delisting haircut + OOS = the FIRST genuinely net-new, cost-positive, survivorship-robust edge
  across all surfaces → FLAG the Lead for a confirmatory disjoint-period replication BEFORE excitement.
- If it survives cost but DIES under the delisting haircut → reported honestly as a survivorship artifact
  (the B4 outcome), still a clean result that quantifies the bias.
- If it dies at net-of-cost even at weekly turnover → the cost-amortization thesis is falsified and the
  multi-day horizon is also null; report cleanly with the break-even cost. No post-hoc horizon/threshold
  tuning; exploration beyond these cells is labeled EXPLORATORY and excluded from the FDR family.

## RUN PLAN (this cycle = design; run gated)
Design + pre-reg committed now. The full run is GATED only if a leg needs the deeper QUOTE tape for a
realistic spread-based cost model (DataIntegrity is backfilling top-500 quotes now); the bar-based version
(bps cost proxy) can run on the existing deep bars immediately. Build script (`build_weekly.py`) +
screen (`screen.py`) stubbed to the design so the run is a turn-key follow-up once the surface is greenlit
(Ben may steer the next surface per the Lead's note). Research-only: NO quantlib / NO fingerprint flip;
READ-ONLY stores; bounded `--rm` sandboxes.
