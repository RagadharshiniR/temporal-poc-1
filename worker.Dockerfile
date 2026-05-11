FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY contract_worker /app/contract_worker

CMD ["python", "-m", "contract_worker.main"]
