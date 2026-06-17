# The overnight-beta strategy container — the certified W11 edge, paper-only, as an auction-slippage
# MEASUREMENT instrument. FROM fp-dev for the runtime deps (alpaca-py, psycopg, numpy, polars). It does NOT
# consume the feature bus (it reads daily bars from the mounted /store/raw/bars for beta estimation), so it
# needs the store mounted read-only at runtime (see docker-compose.strategies.yml). COPY quantlib for the
# shared lib parity, COPY strategies for the container code.
FROM fp-dev
WORKDIR /app
ENV PYTHONPATH=/app
COPY quantlib /app/quantlib
COPY strategies /app/strategies
CMD ["python", "-m", "strategies.overnight_beta"]
