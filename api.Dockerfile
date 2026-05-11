FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api_server /app/api_server
COPY contract_worker /app/contract_worker

CMD ["python", "-m", "api_server.main"]
