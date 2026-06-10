# Build context is repo root so the backfiller shares quantlib with the ingestor
# (the same aggregation code runs on historical ticks — parity by construction).
FROM python:3.12-slim

WORKDIR /app
COPY services/backfiller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/backfiller/main.py .

ENTRYPOINT ["python", "main.py"]
