# Research program — live BACKLOG (Director, never-empty queue). Hunches only; real numbers go to LEADS.md.

## WAVE 1 — RUNNING (2026-06-16)
- W1 factor/price momentum LIQUID portfolio L/S — CPU — running
- W2 PEAD on LIQUID names (item-2.02) — EDGAR — running
- W4 overnight/intraday decomposition LIQUID portfolio — CPU — running
- L1 literature survey (ranks documented liquid-tradeable anomalies) — subagent — running
- W7 GPU/CPU gradient-boosting L/S on the 606-feature store — pending fp-ml image build (in progress)

## WAVE 1 — QUEUED (pre-register on dispatch)
- W3 13F-holdings change / 13D-activist drift on LIQUID targets (EDGAR content, never mined) — HIGH interest
- W5 time-of-day signal-efficacy map (does vwap_dev/qimb efficacy vary by session window? one timed bet/day) — CPU
- W8 autoencoder / representation learning to DISCOVER features (GPU, needs torch image)
- W6 ETF/index-reconstitution flow (needs the public S&P/Russell reconstitution calendar — data ask)
- W9 short-interest/squeeze (likely a data ask — flag; literature-driven)
- W10 lead-lag networks (liquid leaders predict followers) — CPU

## WAVE 2 SEEDS (hunches — promote to pre-registered hypotheses as wave-1 survivors + the L1 survey land)
- Factor MOMENTUM (momentum-of-factors, not just stocks) if W1 alive
- Analyst-revision drift (needs an estimates/revisions feed — data ask)
- Quality/profitability + value as LIQUID portfolio premia (Fama-French / Novy-Marx) — bars + EDGAR XBRL fundamentals
- Post-buyback / post-issuance drift (EDGAR: 8-K / S-1 / SEC forms) — low-turnover event
- Turn-of-month / day-of-week / holiday seasonality as a portfolio overlay (low-turnover)
- Beta-anomaly / low-volatility portfolio (BAB — Frazzini-Pedersen) — liquid, low-turnover, portfolio
- Dispersion / correlation regime as a conditioner on the above
- ML-DISCOVERED non-linear feature combos from W7/W8 importance → new feature proposals
- Cross-asset: SPY/sector-ETF lead → single-name follower (if W10 lead-lag shows structure)
- Pairs / statistical-arbitrage on liquid co-integrated pairs (portfolio, market-neutral, low-gross-exposure)

## DESIGN CONSTRAINT (every item): must be engineered to clear the friction wall —
liquid-scalable OR portfolio-diversified OR low-turnover OR larger-info-shock. No single-name microstructure
takers (cycles 1-2 proved that class is dead). LEADS.md gets an entry ONLY with real OOS net-of-cost +
per-trade-bootstrap numbers.
