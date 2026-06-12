# Build context is the repo root so the image can include the shared quantlib
# package (same code path used by the backfiller) alongside the service.
FROM python:3.12-slim

WORKDIR /app
COPY services/ingestor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/ingestor/app_ingestor ./app_ingestor
COPY services/ingestor/main.py .

# task #11: bake build-time git SHA for content-based running==intended checks.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

CMD ["python", "main.py"]
