FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY budget_bot.py clasificador.py estado_store.py sheets.py webhook.py ./
# Cloud Run inyecta $PORT
CMD exec uvicorn webhook:app --host 0.0.0.0 --port ${PORT:-8080}
