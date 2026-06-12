# Build context is the repo root so the scheduler shares the quantlib package.
FROM python:3.12-slim

WORKDIR /app
COPY services/scheduler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/scheduler/main.py .

# RUNNING==intended provenance (task #11): bake the build-time git SHA into the image so
# freshness is verified by CONTENT, not by clock. Placed after COPY so changing it only
# rebuilds this tiny layer, never the pip-install layer. Injected by `make rebuild`/`make build`.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

CMD ["python", "main.py"]
