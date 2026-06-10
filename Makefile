.PHONY: test up down logs ps rebuild

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
	docker compose build $(S) && docker compose up -d $(S)
