# Feature-platform image (capture + tooling). Mock vs real is selected at runtime, not build.
FROM python:3.12-slim
WORKDIR /app
RUN pip install -q polars 'psycopg[binary]' 'alpaca-py>=0.30,<1.0' 'websockets>=12,<14'
COPY quantlib /app/quantlib
