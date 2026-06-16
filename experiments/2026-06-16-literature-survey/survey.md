# Literature survey — liquid-tradeable equity anomalies (from the L1 lens-explorer, 2026-06-16)

The L1 lens-explorer surveyed academic + practitioner literature for anomalies that are (a) tradeable in
LIQUID large-caps, (b) ideally low-turnover / portfolio-diversifiable, (c) not already killed by our cycles.
References verified against primary publishers. (Reconstructed from the explorer's report — the run did not
persist the file to disk, but the research is its work.)

## TOP 5 liquid-tradeable, ranked (the wave-2 candidate set)

| # | anomaly | reference | liquidity tier | horizon / turnover | data we HAVE | alive? |
|---|---|---|---|---|---|---|
| 1 | **Overnight BETA premium** — high-beta names earn the premium OVERNIGHT, not intraday (a beta tilt captured close→open) | Hendershott–Livdan–Rösch 2020, JFE | **large-cap-ROBUST** (a risk premium, not microstructure) | overnight-only beta tilt, monthly rebalance | OHLC + computable betas | **ALIVE** — structurally hard to arbitrage |
| 2 | **Net share ISSUANCE** — firms that issue underperform, that buy back outperform | Pontiff–Woodgate 2008; Fama–French 2008 (confirmed in BIG stocks) | large/mid-cap | ~12mo, LOW turnover | EDGAR XBRL shares-outstanding + corp-actions | ALIVE |
| 3 | **Industry / SECTOR momentum** — sectors trend; trade via sector ETFs (cheapest friction) | Moskowitz–Grinblatt 1999 | large-cap, lowest-friction (sector ETFs) | 6mo formation, monthly | GICS sectors + bars (needs >12mo history) | partially decayed but friction-cheap |
| 4 | **Akbas "tug-of-war"** — count of days where overnight & intraday returns disagree predicts reversal | Akbas et al. 2022, JFE | moderate (verify by tier) | low-turnover monthly characteristic | close→open / open→close (HAVE; 126d ≈ 6 formation months) | alive, unproven OOS |
| 5 | **FACTOR momentum** — factors themselves trend (momentum of factor returns) | Ehsani–Linnainmaa 2022, JF; Gupta–Kelly 2019 | liquid factor legs | 6–12mo, low–mod turnover | build from bars + 606-vector (needs >12mo) | alive, crowding |

**Single best bets to clear the friction wall: #1 overnight-beta premium and #3 sector momentum** — both
buildable from data in hand (only a daily-bar history extension needed, NO paid acquisition), both documented
to live in liquid large-caps (not the micro-cap tier that trapped cycles 1-2).

## DECAY verdicts — do NOT pursue (saves wasted cycles)

- **Classic SUE PEAD is DEAD in large-caps post-2006** (Martineau 2022). → directly informs our running W2:
  the LIQUID-tier PEAD is likely a null; expect KILL. (Our reaction-sign proxy is also weaker than a true SUE.)
- **Analyst-revision drift** — weakest exactly in liquid high-coverage names + needs I/B/E/S (blocked).
- **S&P 500 inclusion effect decayed 7.4% → 0.3%** (Greenwood–Sammon 2025). → de-prioritizes part of W6;
  the broader index-RECONSTITUTION/rebalance-flow may still have a small mechanical edge but the classic
  inclusion pop is gone.
- **Short-interest / squeeze** — small / illiquid-only; the one robust large-cap member (loan-fee/utilization)
  needs a paid vendor. → W9 is a data-ask, low priority.
- **Seasonality** except turn-of-month is wrong-tier or uncomputable on 126d.

## THE KEY CONSTRAINT (program-level)

Our 126-day bar history can FORM all these tilts live but **cannot CERTIFY any multi-month premium in-house.**
Backfilling daily bars to **≥18 months** is the single highest-leverage unlock for #2/#3/#5 (and would let
W1 momentum + W3 13D be tested with real depth). → a concrete DATA ASK to the Lead/backfill agent: extend the
daily-bar history to ≥18 months (cheap — daily bars are tiny vs the tick backfill).

## How this reshapes the program (Director synthesis)

- **NEW wave-2 leads (promote to pre-registered hypotheses):** W11 = overnight-beta premium (#1 — distinct
  from W4's plain overnight level, which we just KILLED as survivorship; this is a BETA tilt, a risk premium);
  W12 = net-share-issuance (#2 — EDGAR XBRL, low-turnover, the cleanest fundamental); W13 = sector momentum
  (#3 — sector ETFs, lowest friction). All three need the ≥18-month bar history to certify, so they pair with
  the data ask.
- **W2 (PEAD)**: the survey predicts a large-cap null — consistent with our liquid-gate design; await its
  verdict but low expectation.
- **W6 (reconstitution)**: down-weight the classic inclusion pop; keep only the mechanical rebalance-flow angle.
- **W9 (short-interest)**: confirmed low-priority data-ask.
