# docs/archive

Superseded design/operator docs, kept for historical reference (git history preserved via `git mv`).
These describe how a subsystem USED to work; they are NOT the current design of record. Each archived
doc carries a banner naming what superseded it and where the live doc/code lives.

| Archived | Superseded by | When |
|---|---|---|
| `TRUST_METADATA.md` | binary-trust redesign — `docs/TRUST_REDESIGN.md` + `quantlib/features/trusted_list.py` (`trust_state='TRUSTED'`) | 2026-06-17 |
| `RESUME_STATE.md` | team-of-agents operating model — `~/.quant-ops/SYSTEM_LOG.md` (live state) + `docs/OPERATIONS.md` (cron registry) + `docs/OPERATING_MODEL.md` | 2026-06-19 |
| `MONDAY_COLLECT_SAVE.md` | live daily capture is operational — `docs/OPERATIONS.md` + `docs/SESSION_WARMUP.md` + `~/.quant-ops/SYSTEM_LOG.md` (per-Monday plan); T+1 parity is `docs/PARITY_PLAYBOOK.md` | 2026-06-20 |
| `ARCHITECTURE.md` | navigable doc hierarchy — `docs/SYSTEM_DESCRIPTION.md` (#293) as the whole-system map; live state in `~/.quant-ops/SYSTEM_LOG.md`; vision in `docs/MISSION.md` + `docs/FEATURE_PLATFORM.md` (self-deprecated 2026-06-10 pre-pivot snapshot, dead ~40-feat Phase 0–7 ladder) | 2026-06-20 |
