FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Data now lives in a Dokploy-managed Postgres (DATABASE_URL is injected as an
# env var by Dokploy at runtime). No local SQLite file / volume needed — this
# is what removes the old `VOLUME ["/data"]` anonymous-volume wipe trap.
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
