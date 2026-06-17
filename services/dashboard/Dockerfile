# Dashboard image. Build context is the REPO ROOT (see docker-compose.yml) so the image can COPY the
# shared `quantlib` package and the `rust/` source — the feature-coverage grid imports the LIVE feature
# registry + store introspection (quantlib.features) rather than re-encoding the catalog, and that import
# chain needs the compiled `quant_tick` Rust kernel (two feature groups use it). Same multi-stage recipe
# as docker/fp-dev.Dockerfile: stage 1 builds the quant_tick wheel, stage 2 is the lean runtime.
# Rebuilding THIS container has no feature-compute fingerprint risk (it never computes/serves features).

FROM python:3.12-slim AS rustbuild
RUN apt-get update && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
ENV PATH="/root/.cargo/bin:${PATH}"
RUN pip install --no-cache-dir maturin
COPY rust /build/rust
RUN cd /build/rust && maturin build --release --out /wheels

FROM python:3.12-slim
WORKDIR /app
# Dashboard web deps + the quantlib runtime deps the introspection import chain needs (polars/numpy +
# psycopg for the trust DB).
COPY services/dashboard/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# quant_tick (the compiled Rust kernel) from the wheel built above.
COPY --from=rustbuild /wheels/*.whl /tmp/
RUN pip install /tmp/*.whl && rm /tmp/*.whl

COPY quantlib ./quantlib
COPY services/dashboard/app.py services/dashboard/feature_grid.py services/dashboard/feature_grid_page.py ./

# Default store mount (read-only fp_store_real in compose). Overridable per environment.
ENV STORE_ROOT=/store

# task #11: bake build-time git SHA for content-based running==intended checks.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
