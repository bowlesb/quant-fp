# Build context is the repo root so the image can include the shared quantlib
# package (same code path used by the backfiller) alongside the service.
FROM python:3.12-slim

WORKDIR /app
COPY services/ingestor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/ingestor/main.py .

CMD ["python", "main.py"]
