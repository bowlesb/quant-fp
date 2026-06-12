# Build context is repo root so the service shares the quantlib package.
FROM python:3.12-slim

WORKDIR /app
COPY services/backfill-manager/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/backfill-manager/main.py .

# task #11: bake build-time git SHA for content-based running==intended checks.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

CMD ["python", "main.py"]
