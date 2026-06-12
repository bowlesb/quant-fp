.PHONY: test up down logs ps rebuild rebuild-all build-fresh check-fresh

# Build-time provenance (task #11): stamp the current git SHA into every image so
# running==intended is verifiable by content. Appends -dirty if the working tree is unclean
# (an image built from uncommitted code must NEVER read as authoritative).
# -dirty if the worktree has EITHER unstaged OR staged uncommitted changes — a baked SHA must never
# read "clean" when the repo isn't at that exact commit (staged-only drift fooled the old unstaged-only
# check; exposed by the EXPERIMENTS.md staged-in-shared-index incident 2026-06-12).
GIT_SHA := $(shell git rev-parse --short HEAD)$(shell git diff --quiet 2>/dev/null && git diff --cached --quiet 2>/dev/null || echo -dirty)

# Run the shared-library tests (parity + aggregation) in a clean container.
test:
	docker run --rm -v "$$PWD":/app -w /app python:3.12-slim \
		sh -c "pip install -q pytest && python -m pytest tests -q"

up:
	docker compose up -d

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f --tail=50 $(S)

# Rebuild and restart one service: make rebuild S=ingestor
rebuild:
	docker compose build --build-arg GIT_SHA=$(GIT_SHA) $(S) && docker compose up -d $(S)

# Rebuild and restart EVERY service with the current SHA baked in (post-close batch).
rebuild-all:
	docker compose build --build-arg GIT_SHA=$(GIT_SHA) && docker compose up -d

# Tonight's batch rebuild (b856aa7 absorption): GIT_SHA-stamp + restart all long-running BUILT
# services EXCEPT executor — execution-risk owns the executor deploy via `make rebuild S=executor`
# AFTER #19 review+bless (that one targeted restart folds in the #18 ex-date guard + #19). This way
# the ingestor still restarts exactly once and we never deploy un-approved #19.
BATCH_SERVICES := ingestor scheduler feature-computer model-server backfill-manager experimenter dashboard
rebuild-batch:
	docker compose build --build-arg GIT_SHA=$(GIT_SHA) $(BATCH_SERVICES)
	docker compose up -d $(BATCH_SERVICES)

# Build one or all services with the SHA baked in, no restart: make build-fresh [S=ingestor]
build-fresh:
	docker compose build --build-arg GIT_SHA=$(GIT_SHA) $(S)

# Audit running==intended for all (or one) service: make check-fresh [S=scheduler]
check-fresh:
	scripts/assert_image_fresh.sh $(S)
