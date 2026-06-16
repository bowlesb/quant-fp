# Research Program — expansive, parallel, friction-wall-by-design (Director plan)

**2026-06-16.** Ben's redirect: stop holding, run a DIVERSE PARALLEL program using ALL assets (unlimited
Alpaca paper, the fully-backfilled feature store, unlimited internet/papers/subagents, the RTX 3090). The
cycle-1/2 structural law — **every real signal we found was smaller than the friction at the liquidity tier
where it lives** — is now the DESIGN CONSTRAINT, not a dead end. So every hypothesis in this program is
deliberately engineered to clear the friction wall via one or more of: **liquid-scalable** (megacaps/ETFs,
0.4–3 bps spread), **portfolio-diversified** (cross-sectional L/S nets idiosyncratic noise so a small per-name
edge survives at portfolio scale), **low-turnover / longer-horizon** (cost amortized over a bigger move), and
**larger-information-shock signals** (fundamentals/events re-priced over days, not microstructure wiggles).

## Assets (verified)
- **GPU:** RTX 3090, 24 GB, idle (confirmed `nvidia-smi`), `fp-gpu` image exists → gradient-boosting + deep
  nets + representation learning.
- **Data:** trades 63d (~7,600 sym), quotes 63d (~4,000 sym), bars (126d), EDGAR **3.2M PIT filings**
  (content unmined — only the 8-K flag used so far), 36k corporate actions, the 606–610 live feature vectors.
- **Live paper:** unlimited Alpaca paper → run MANY paper strategy containers in parallel, gather live
  evidence at scale (not one container).
- **Compute:** `ops/sandbox.sh` (CPU/mem-capped) + GPU lock for ML; subagents for literature + parallel runs.

## NON-NEGOTIABLE rigor (every hypothesis, no exceptions)
Pre-register BEFORE running · shuffle-canary · per-symbol demean (survivorship) · walk-forward OOS hold-out
for anything post-hoc · cost + LIQUID-tier gate (measured spread) · **per-trade/per-fill bootstrap on
realized NON-OVERLAPPING trades** (the HF02/HF03 lesson — an IC or aggregate "net" is not tradeable P&L).
RESEARCH_PITFALLS.md (8 entries) is the standing checklist. BACKLOG.md = hunches; LEADS.md = real numbers only.

## The DIMENSIONS (lenses) + how each attacks the friction wall

| # | Lens | Friction-wall design | Asset |
|---|---|---|---|
| L1 | **Literature survey** → liquid-tradeable anomalies | only pre-register anomalies documented in LIQUID names | subagents + internet |
| L2 | **Cross-sectional PORTFOLIO L/S** (factors) | diversification: small per-name edge survives at portfolio scale | feature-mining + GPU |
| L3 | **ML / GPU** non-linear + learned features | find non-linear combos a linear IC misses; importance → new features | GPU |
| L4 | **Time-of-day / timed bets** | the SAME signal may pay at a specific session window (open/close auction, EOD) | feature-mining + paper |
| L5 | **EDGAR fundamentals CONTENT** (13F/13D/XBRL) | day-horizon info shock in LIQUID names = larger move vs friction; low turnover | EDGAR + GPU |
| L6 | **Event/flow** (ETF-rebalance, index-recon, short-interest) | liquid-by-construction + mechanical pressure | data + paper |
| L7 | **Overnight vs intraday decomposition / seasonality** | low-turnover calendar structure; portfolio-applied | feature-mining |

## FIRST WAVE — 10 pre-registered hypotheses (each gets its own hypothesis.md before running)

Friction-wall tag in [brackets]. Each is designed to clear cost by construction.

1. **W1 · Factor-momentum cross-sectional L/S (L2)** — rank megacaps+large-caps by trailing
   3/6/12-mo (and intraday-composite) return; decile L/S, monthly/weekly rebalance. [portfolio-diversified +
   low-turnover + liquid] — the canonical academic factor; the design question is whether the LIQUID-tier L/S
   clears cost at low turnover. *feature-mining (bars 126d) → if alive, GPU non-linear.*
2. **W2 · PEAD on LIQUID names, item-2.02-confirmed (L1/L5)** — the earnings subset of 8-Ks (item 2.02,
   parsed from the submissions `items` field) restricted to the liquid tertile, drift D+1→D+10. [info-shock +
   low-turnover + liquid-gated] — cycle-1 H10 pooled all 8-Ks + died illiquid; this isolates the documented
   PEAD in tradeable names. *EDGAR + bars.*
3. **W3 · 13F institutional-holdings change / 13D activist on LIQUID targets (L5)** — quarter-over-quarter
   13F holdings deltas + 13D activist-stake events; forward drift on the LIQUID targets. [info-shock +
   low-turnover + liquid] — never mined; a real information event, not a calendar effect. *EDGAR content.*
4. **W4 · Overnight vs intraday return decomposition (L7)** — split each name's return into
   close→open (overnight) and open→close (intraday); is there a persistent, portfolio-diversifiable
   premium in one component, survivorship-demeaned, in liquid names? [portfolio + low-turnover] — the
   overnight-anomaly literature, tested clean (cycle-0 found overnight was survivorship; portfolio + demean
   is the honest re-test). *bars 126d.*
5. **W5 · Time-of-day signal efficacy map (L4)** — take the qimb/vwap_dev signals (real but friction-killed)
   and ask: does their predictive efficacy / net-of-cost vary by SESSION WINDOW (open-auction, first 30m,
   mid-day, last 30m, close-auction)? Is there a timed window where a slow signal + a single timed entry/exit
   clears cost? [low-turnover via ONE timed bet/day]. *trades/quotes 63d.*
6. **W6 · ETF / index-reconstitution flow (L6)** — build the S&P/Russell add/drop calendar (public);
   pre/post-effective-date drift on the (liquid-by-construction) added/dropped names. [liquid + mechanical +
   low-turnover]. *needs the reconstitution calendar (public scrape) — Ben's data folded in.*
7. **W7 · GPU gradient-boosting on the 606-feature vector for a LIQUID L/S (L3)** — train GBM/XGBoost to
   predict forward 1d/5d return on the live feature store, restricted to liquid names; turn the prediction
   into a decile L/S; the per-trade bootstrap + cost gate decide it. [portfolio + ML-nonlinear + liquid].
   *GPU. Feature-importance → candidate new features.*
8. **W8 · Autoencoder / representation learning to DISCOVER features (L3)** — train an autoencoder on the
   feature-vector + raw microstructure to learn a compressed representation; test whether the learned latent
   factors predict forward returns beyond the raw features (a feature-discovery probe). [ML-discovery, feeds
   L2/L7]. *GPU.*
9. **W9 · Short-interest / squeeze proxy (L1)** — without a short-interest feed yet, proxy squeeze pressure
   from borrow/volume/volatility patterns (and flag the real data ask); pre-register the documented
   short-squeeze drift in liquid-enough names. [event + portfolio] — literature-driven; likely a DATA ASK,
   pre-registered honestly. *feature-mining + literature.*
10. **W10 · Lead-lag network (L1)** — do liquid "leader" names (e.g. sector bellwethers, SPY) predict
    "follower" returns at a short lag, tradeably? [liquid + portfolio cross-sectional]. *bars/trades —
    the documented lead-lag literature, tested net-of-cost.*

## Wave-1 execution assignment (parallel)

- **GPU lane (serialized via `~/.quant-gpu.lock`):** W7 (GBM L/S) FIRST (most direct EV — non-linear on the
  feature store we already trust), then W8 (autoencoder discovery). One GPU job at a time.
- **CPU feature-mining lane (parallel sandboxes):** W1 (factor momentum), W4 (overnight/intraday), W5
  (time-of-day map) — all on bars/ticks we have, no new data.
- **EDGAR-content lane:** W2 (PEAD-liquid) + W3 (13F/13D) — parse the submissions `items` + 13F XML; CPU.
- **Literature lane (subagents, no compute):** L1 survey → W6 (reconstitution calendar feasibility), W9
  (short-interest), W10 (lead-lag) — survey + pre-register; flag data asks.
- **Live-paper lane:** once any wave-1 hypothesis clears the gates OOS, stand up a PAPER container for it
  (alongside smoke + reversion) to gather live evidence — many small paper strategies in parallel, capped.

## Sequencing + cadence

Wave 1 = the 10 above. I dispatch the CPU/EDGAR/literature lanes in parallel immediately (pre-register each
first), and grant the GPU to W7. As verdicts land I (the Director) synthesize into LEADS.md, kill/keep, and
seed Wave 2 from the survivors + the literature survey's best leads — never-empty queue. Anything that
clears all gates OOS + per-trade bootstrap → a feature proposal (parity path) AND a paper container.

## What "clears the wall" means (the bar, restated)

A KEEP requires, in the LIQUID tier, net-of-measured-cost positive after the per-trade/per-fill bootstrap CI
excludes zero, OOS, surviving the demean + canary — AND a capacity ≥ ~$100K (trivially met for liquid L/S).
A portfolio L/S that nets positive at the PORTFOLIO level even with a tiny per-name edge is the explicit
target. We are no longer hunting a big single-name signal; we are hunting a DIVERSIFIABLE or LOW-TURNOVER or
LIQUID-MECHANICAL edge that is designed to survive cost.
