# Overnight-beta paper container — design (for Lead review BEFORE deploy)

The W11 overnight-beta premium is CERTIFIED at research but AUCTION-COST-GATED: the OOS net INCLUDING a 5 bps
MOO/MOC auction-slippage stress straddles zero. The ONE number that decides whether this is a real
deployable edge is the REAL auction slippage — which a backtest can only model. **This container's primary
job is to MEASURE that number** by trading the strategy paper-only and logging model-expected vs realized
auction fills.

## What it trades (the certified strategy, paper only)

- **Universe:** top ~150–200 liquid single stocks by dollar-volume, **excluding the crypto/AI speculation
  cohort** (the certification's confound control — COIN/MARA/HOOD/IONQ/CLSK/AFRM/ASTS/APP/BBAI/CEG/GEV/CCJ/
  APLD/CIFR and similar). Env-driven list (`OBETA_SYMBOLS` / `OBETA_EXCLUDE`).
- **Signal:** per name, market beta = rolling 60-day OLS of the name's daily return on SPY; re-estimated
  monthly. Sort into beta quintiles. The bet = LONG the top-beta quintile, SHORT the bottom-beta quintile,
  equal-weight, dollar-neutral.
- **Timing (the whole point):** ENTER at the CLOSE auction (Alpaca `TimeInForce.CLS` = market-on-close),
  HOLD OVERNIGHT, EXIT at the next OPEN auction (`TimeInForce.OPG` = market-on-open). This captures exactly
  the overnight (close→open) leg the premium lives in, and the auction TIFs are how a small book gets the
  auction price. Rebalance the quintile membership monthly (turnover ~18%).
- **Paper only**, tiny notional per name, the same safety caps as smoke/reversion (kill switch, market-hours
  gating for the order submission window, max gross exposure, max names), its OWN `strat_overnightbeta`
  Postgres schema via `StrategyStore`.

## What it MEASURES (the deliverable that gates capital)

For every auction fill, log: the model-EXPECTED price (the last quote mid / official close-or-open print the
model assumed) vs the REALIZED fill price Alpaca returns for the MOC/MOO order → **realized auction slippage
in bps**, per name, per side, accumulated. After ~1–2 weeks of paper overnights it answers: is the real
MOO/MOC slippage below the ~5 bps the certification needs? If yes → the +20–35 bps overnight net is real and
we escalate (more names, more time, then a capital proposal to Ben). If the real slippage eats the edge →
the premium is real but un-harvestable at auction, and we say so (the honest negative).

## Why a container (not just more backtest)

The backtest CANNOT measure real auction slippage — it modeled 5 bps as a stress. Alpaca paper executes real
MOC/MOO orders against the real auction and reports the real fill. So this container is a MEASUREMENT
INSTRUMENT first, a strategy second. It is the correct, cheap (paper) way to resolve the one open question.

## Build plan (code, for the PR — NOT deployed until Lead review)

1. `strategies/lib/overnight_beta_model.py` — `OvernightBetaModel`: from a panel of recent daily returns +
   SPY, compute per-name 60d beta, return the high-quintile (long) / low-quintile (short) name sets. A pure,
   testable function (no I/O).
2. `strategies/overnight_beta/` — the container: `bet_store.py` (its `strat_overnightbeta` schema: positions
   + the auction-slippage log table), `strategy.py` (the close-auction-enter / open-auction-exit loop, the
   safety gate, the slippage logging), `__main__.py`, `Dockerfile`. Built on the reversion-container template.
3. A compose service `overnight-beta-strategy` (paper, capped, env-driven, off by default `OBETA_ENABLED=0`).
4. Unit tests: the beta-quintile model (deterministic on a known panel), the pure safety gate, the
   slippage-computation math.

## Safety / scope

Paper-only (Alpaca paper account, the same as smoke/reversion). No real capital. Caps: kill switch
(`OBETA_ENABLED=0` default until reviewed), max gross notional, max names per leg, market-hours-gated
submission. Secrets via env, never logged. Its own schema — no collision. Deploy only after Lead review +
explicit go, alongside (not replacing) smoke + reversion.
