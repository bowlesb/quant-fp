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

### In progress / next
- [ ] Ingestor service: Alpaca SIP websocket → bars_1m for a small symbol set → DB.
      (Requires stopping the legacy Edgar streamer first — single Alpaca websocket.)
- [ ] Hello-world loop: one paper order/day, logged fill, reconciliation.
- [ ] Scheduler: daily coverage report + reconciliation job.

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
