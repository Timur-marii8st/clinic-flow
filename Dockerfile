FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --uid 10001 clinic && mkdir -p /app/data && chown clinic /app/data
COPY app ./app
COPY alembic.ini .
COPY migrations ./migrations
COPY scripts ./scripts
USER clinic
ENV CLINIC_DB_PATH=/app/data/clinic.sqlite3 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
