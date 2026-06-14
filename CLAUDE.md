# quant-fp — Claude Code instructions

## Dev/test environment — USE THE BAKED `fp-dev` IMAGE (do NOT pip-install per run)

All Python runs (tests, benches, catalog) go through a Docker image with deps baked in. **Never**
`docker run python:3.12-slim sh -c "pip install ..."` per command — that re-downloads polars/pytest
every time. Build the image once, then reuse it:

```bash
make dev-image          # one-time (or after a dependency change): builds the `fp-dev` image
# then, for any run:
docker run --rm -v "$PWD":/app -w /app fp-dev python -m pytest tests/ -q
docker run --rm -v "$PWD":/app -w /app fp-dev python -m quantlib.features.profile 2000 120 250 3
```

Convenience targets (all use `fp-dev`): `make test-fp`, `make feature-catalog`, `make fp-bench`,
`make fp-profile`, `make parity DAY=...`. The image is defined in `docker/fp-dev.Dockerfile`.

## Hard rules
- **Parity is sacred.** A feature computed LIVE must equal the value computed from BACKFILL within
  the declared tolerance (~1e-6 rel). Speed must never compromise this. One code path live + backfill;
  any second/fast path (aggregate-at-T, etc.) ships ONLY behind a `latest == rolling.last()` parity
  test. See `docs/PARITY_PLAYBOOK.md`.
- **Timed and fast.** Every feature must pass the latency gate (`tests/test_fp_latency.py`). Profile
  every feature batch (`make fp-profile`). Scalability is a first-class edge — see `docs/SCALABILITY.md`.
- Temporary investigation scripts go in `/tmp`, never committed.

## Key docs
- `docs/FEATURE_PLATFORM.md` — vision, requirements, milestones (the spine).
- `docs/SCALABILITY.md` — the <100ms-per-minute design (timing + process-split + latest-minute).
- `docs/FEATURE_TAXONOMY.md` — the all-encompassing feature roadmap (built + proposed, bucketed A/B/C).
- `docs/PARITY_PLAYBOOK.md` — the live↔backfill parity rules + the trailing-buffer invariant.
