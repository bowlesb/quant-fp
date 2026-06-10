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

### In progress / next
- [ ] Decide baseline: flatten stray paper positions (DLTR) so reconciliation is
      green, OR leave as-is. (Awaiting Ben — it's account state.)
- [ ] Scheduler: daily coverage report job + reboot-survival check.
- [ ] Phase 0 gate: 5 clean days, unattended reboot survival, daily report.
- [ ] Then Phase 1: scale ingestor to ~1,000-symbol universe + add trades/quotes/news.

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
