# Build context is repo root so the service shares the quantlib package (same
# feature code as the historical builder — parity by construction).
FROM python:3.12-slim

WORKDIR /app
COPY services/feature-computer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY quantlib ./quantlib
COPY services/feature-computer/main.py .

CMD ["python", "main.py"]
