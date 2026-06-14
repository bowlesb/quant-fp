# Dev/test image for the feature platform — deps baked in so we NEVER pip-install per run.
# Build once:  make dev-image   (or: docker build -t fp-dev -f docker/fp-dev.Dockerfile docker)
# Use:         docker run --rm -v "$PWD":/app -w /app fp-dev python -m pytest tests/ -q
FROM python:3.12-slim
RUN pip install --no-cache-dir \
    polars \
    pytest \
    websockets \
    numpy \
    'psycopg[binary]' \
    'alpaca-py>=0.30,<1.0'
WORKDIR /app
