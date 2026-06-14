# Dev/test image for the feature platform. Multi-stage: stage 1 compiles the Rust extension
# (quant_tick) into a wheel; stage 2 is the lean Python runtime with deps + the wheel baked in, so we
# NEVER pip-install per run AND the Rust kernel is always importable.
# Build context is the REPO ROOT (so it can COPY rust/):  make dev-image
# Use:  docker run --rm -v "$PWD":/app -w /app fp-dev python -m pytest tests/ -q

FROM python:3.12-slim AS rustbuild
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"
RUN pip install --no-cache-dir maturin
COPY rust /build/rust
RUN cd /build/rust && maturin build --release --out /wheels

FROM python:3.12-slim
RUN pip install --no-cache-dir \
    polars \
    pytest \
    numpy \
    websockets \
    prometheus-client \
    'psycopg[binary]' \
    'alpaca-py>=0.30,<1.0'
COPY --from=rustbuild /wheels/*.whl /tmp/
RUN pip install /tmp/*.whl && rm /tmp/*.whl
WORKDIR /app
