# STATE — read this first

**Phase:** 0 (Foundation & Skeleton) — in progress
**Mode:** paper
**Last updated:** 2026-06-10

## Current status

Fresh build started. Repo scaffolded. Design captured in `ARCHITECTURE.md`.

### Done
- Repo structure + git init.
- `ARCHITECTURE.md` (committed design, source of truth).
- TimescaleDB schema (`db/init/01_schema.sql`) — verified: 14 tables, 7 hypertables.
- `docker-compose.yml` with TimescaleDB + dashboard + Prometheus + Grafana.
- `.env` with Alpaca **paper** keys (gitignored); `.env.example` committed.
- **Dashboard** (`services/dashboard`) live on the LAN at http://192.168.1.32:8088 —
  renders STATE.md/JOURNAL.md progress + live DB health, auto-refresh 30s. This is
  how Ben monitors (no Discord/Telegram/tunnel; Claude reads DB directly in-session).

- **Ingestor** (`services/ingestor`) live: Alpaca **SIP** websocket → `bars_1m`,
  10 liquid symbols, source='stream'. Verified end-to-end (SIP→DB→dashboard).
  SIP entitlement (Algo Trader Plus) confirmed active on the account.
- **Legacy Edgar Docker stack torn down** (`docker compose down`, volumes kept);
  only the `quant` stack runs now, freeing the single Alpaca data websocket.

- **Executor** (`services/executor`) live: places one tiny paper order/day,
  records order+fill, and reconciles DB positions vs Alpaca /positions every 5 min.
  Verified: 1-share SPY filled @730.52, recorded; reconciliation works and flagged
  a stray DLTR(1) paper position left over from the old Edgar system.

- **Paper account reset to clean baseline** (Ben approved): all positions flattened,
  open orders cancelled, test order/fill/recon rows truncated. DB and broker now
  agree from zero; executor re-establishes a consistent daily order each cycle.
- **Scheduler** (`services/scheduler`) live: computes per-symbol coverage
  (received vs expected 1-min bars) for the current/last session into
  data_quality_daily; dashboard shows a Coverage panel. (First partial day reads
  low % because we started mid-session; full days from start onward read ~100%.)

### Phase 0 status: all 7 services built, healthy, and survive teardown/restart.
- Reboot survival: Docker enabled on boot + `restart: unless-stopped` on every
  service; full `compose down && up` verified — data persisted (bars 110→120, no
  loss), all services returned healthy. Prom/Grafana data moved to named volumes
  (bind-mount permission fix). Executor made idempotent across DB resets (broker
  is source of truth for "ordered today") and order errors no longer crash-loop.
- Remaining for the gate: accumulate ~5 clean trading days of coverage. A true
  host-reboot test can be run anytime (low risk) — say the word.

### Phase 1: in progress
- [x] **Shared `quantlib` aggregation library** (the parity cornerstone): per-minute
      trade & quote aggregates, pure/deterministic, with a live-vs-batch parity
      test (`make test`, 5 passing). Both ingestor and (future) backfiller call it.
- [x] **Ingestor extended** to trades + quotes via quantlib → trade_agg_1m,
      quote_agg_1m, trades_raw (30-day rolling). Verified live: realistic signed
      volume, ~1-4 bps spreads, ~11k raw trades/min across 10 symbols.
- [x] **docs/RESEARCH.md** — 40-item ML-approaches backlog (rings 1–4 + methodology).
- [x] **Universe construction** in scheduler: screens ~12.7k tradable equities by
      price>$5 and ADV$>$10M, keeps the most-liquid ≤1,000 into universe_membership
      (point-in-time, per trade_date). Pure selection in quantlib.universe (tested);
      runs once/day. Spread filter is a later refinement.
- [ ] News stream → news table (lower priority; collection-now-model-later).
- [x] **Backfiller** (`services/backfiller`, run-on-demand tool) + **validate-bars**
      gate. Verified: backfilled today's 10 symbols (4,736 bars, source='backfill')
      and compared to streamed — 99.76% OHLC / 95% incl. volume; mismatches are
      benign late-corrections (±1 print). Finding logged in JOURNAL: treat backfill
      as authoritative for training; stream is for live trading.
      Run: `docker compose --profile tools run --rm backfiller backfill-bars|validate-bars`.
- [ ] Validate on a fully-settled prior day for the official ≥99.9% gate number.
- [ ] Backfill trade/quote AGGREGATES through quantlib (the parity-on-real-data
      proof) + extend validation to aggregates.
- [ ] 6-year historical backfill across the universe (disk now available).
- [ ] Scale live ingestion from 10 → ~1,000 symbols (batched async writes).
- Prereq for backfill: free SSD headroom (move recovered files off — task #3,
  awaiting Ben's OK to wipe sdb).

## Known constraints / decisions
- Deploy target: this Intel box. TimescaleDB host port **5433**, Grafana **3001**,
  Prometheus **9091** — chosen to avoid colliding with the legacy Edgar stack still
  running on the default ports.
- **Single Alpaca data websocket per account:** before the new ingestor streams,
  the legacy Edgar streamer must be stopped or it will contend for the connection.
- SSD currently ~85% full (file-recovery side task). Phase 1's 6yr backfill needs
  the recovered data moved off first — see `reference-disk-and-recovery` memory.

## Open items needing Ben
- Alert channel: Discord or Telegram? (for daily reports + alerts)
- Remote status access: Cloudflare Tunnel or Tailscale? (so Claude can read
  `/status.json` from any session)
- Approve Algo Trader Plus (~$99/mo SIP) when Phase 1 begins.
