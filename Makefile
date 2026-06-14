.PHONY: test up down logs ps rebuild rebuild-all build-fresh check-fresh

# Build-time provenance (task #11): stamp the current git SHA into every image so
# running==intended is verifiable by content. Appends -dirty if the working tree is unclean
# (an image built from uncommitted code must NEVER read as authoritative).
# -dirty reflects uncommitted IMAGE-RELEVANT source only (staged OR unstaged) — what actually gets
# COPYed into an image + its build inputs. NOT docs/ ledgers or experiments/ output, which the
# never-idle grind writes continuously and which never enter an image (so they must not false-flag it).
# This is the principled #11 stamp: an image is -dirty iff its SOURCE is uncommitted, period.
IMG_SRC := services quantlib docker-compose.yml Makefile
GIT_SHA := $(shell git rev-parse --short HEAD)$(shell git diff --quiet -- $(IMG_SRC) 2>/dev/null && git diff --cached --quiet -- $(IMG_SRC) 2>/dev/null || echo -dirty)

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
	@echo "rebuild-batch: asserting running==intended (task #11 BLOCKING gate)"
	scripts/assert_image_fresh.sh $(BATCH_SERVICES)

# Run a one-shot tools-profile container through the BLOCKING freshness gate (task #11) — never run
# stale code (the 4th near-miss was a 14h-stale trainer). Rebuilds-if-stale then runs; env passes
# through. Usage: make run-tool S=backfiller A="fetch-corporate-actions"
run-tool:
	scripts/run_tool.sh $(S) $(A)

# Build one or all services with the SHA baked in, no restart: make build-fresh [S=ingestor]
build-fresh:
	docker compose build --build-arg GIT_SHA=$(GIT_SHA) $(S)

# Audit running==intended for all (or one) service: make check-fresh [S=scheduler]
check-fresh:
	scripts/assert_image_fresh.sh $(S)

# --- Feature platform (FEATURE_PLATFORM.md) ---
.PHONY: dev-image feature-catalog introspect parity test-fp fp-bench fp-profile

# Baked dev/test image — deps installed ONCE so we NEVER pip-install per run.
# ALL feature-platform docker runs use `fp-dev`. Build/refresh it after a dependency change.
dev-image:
	docker build -t fp-dev -f docker/fp-dev.Dockerfile .

FP_RUN := docker run --rm -v "$$PWD":/app -w /app
FP_DB := --network quant_default --env-file .env

# Regenerate docs/FEATURES.md from the registry (drift-gated in CI)
feature-catalog:
	$(FP_RUN) fp-dev python -m quantlib.features.catalog docs/FEATURES.md

# T+1 Settled-Day Parity for a day:  make parity DAY=2026-06-12
parity:
	$(FP_RUN) $(FP_DB) fp-dev python -m quantlib.features.parity $(DAY)

# Introspect a day's features:  make introspect DAY=2026-06-12 [SOURCE=backfill]
introspect:
	$(FP_RUN) $(FP_DB) fp-dev python -m quantlib.features.audit $(DAY) $(SOURCE)

# ALL FP unit tests (run before reporting done)
test-fp:
	$(FP_RUN) fp-dev python -m pytest tests/test_fp_platform.py tests/test_fp_store.py tests/test_fp_new_families.py tests/test_fp_sharding.py tests/test_fp_latest.py tests/test_fp_latency.py -q

# Scale + per-group latency benches:  make fp-bench [N=10000] ;  make fp-profile [N=2000]
fp-bench:
	$(FP_RUN) fp-dev python -m quantlib.features.mem_bench $(or $(N),10000) 120 250 1000
fp-profile:
	$(FP_RUN) fp-dev python -m quantlib.features.profile $(or $(N),2000) 120 250 3
