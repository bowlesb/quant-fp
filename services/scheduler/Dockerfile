# Build context is the repo root so the scheduler shares the quantlib package.
FROM python:3.12-slim

WORKDIR /app
COPY services/scheduler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/scheduler/main.py .

CMD ["python", "main.py"]
