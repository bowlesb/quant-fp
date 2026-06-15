# Build context is the repo root so the EDGAR ingestor shares the quantlib package
# (the SEC token-bucket lives in quantlib for stream/backfill parity).
FROM python:3.12-slim

WORKDIR /app
COPY services/edgar/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/edgar/main.py .

# task #11: bake build-time git SHA for content-based running==intended checks.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

CMD ["python", "main.py"]
