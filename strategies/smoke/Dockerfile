# The smoke strategy container. FROM fp-dev for the runtime deps (redis, alpaca-py, psycopg, numpy,
# polars). We COPY quantlib from THIS repo so the bus schema fingerprint is built from the exact same
# feature registry as the producer (a mismatched feature set fails decode loudly rather than misreading
# the stream) — and so the build doesn't depend on quantlib happening to be baked into the base tag.
FROM fp-dev
WORKDIR /app
ENV PYTHONPATH=/app
COPY quantlib /app/quantlib
COPY strategies /app/strategies
CMD ["python", "-m", "strategies.smoke"]
