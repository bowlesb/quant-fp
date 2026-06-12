FROM python:3.12-slim

# lightgbm needs libgomp at runtime
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/trainer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/trainer/main.py .

# task #11: bake build-time git SHA for content-based running==intended checks.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

ENTRYPOINT ["python", "main.py"]
