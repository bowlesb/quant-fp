# Build context is repo root so the executor shares the quantlib package.
FROM python:3.12-slim

WORKDIR /app
COPY services/executor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/executor/main.py .

CMD ["python", "main.py"]
