FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -q -r requirements.txt
COPY server.py .
EXPOSE 9001
CMD ["python", "server.py"]
