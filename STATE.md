# STATE — read this first

**Phase:** 0 (Foundation & Skeleton) — in progress
**Mode:** paper
**Last updated:** 2026-06-10

## Current status

Fresh build started. Repo scaffolded. Design captured in `ARCHITECTURE.md`.

### Done
- Repo structure + git init.
- `ARCHITECTURE.md` (committed design, source of truth).
- TimescaleDB schema (`db/init/01_schema.sql`).
- `docker-compose.yml` with TimescaleDB + Prometheus + Grafana.
- `.env` with Alpaca **paper** keys (gitignored); `.env.example` committed.

### In progress / next
- [ ] Bring up TimescaleDB, apply schema, verify tables (this is the immediate next check).
- [ ] Ingestor service: Alpaca websocket → bars_1m for a small symbol set → DB.
- [ ] Status endpoint (`/status.json`) + daily report job.
- [ ] Hello-world loop: one paper order/day, logged fill, reconciliation.

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
