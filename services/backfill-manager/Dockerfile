# Build context is repo root so the service shares the quantlib package.
FROM python:3.12-slim

WORKDIR /app
COPY services/backfill-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/backfill-manager/main.py .

CMD ["python", "main.py"]
