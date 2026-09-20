# Cloud Run job image. Build context is the repo root:  docker build -f jobs/weekly_pipeline/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY jobs/weekly_pipeline/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY jobs/__init__.py ./jobs/__init__.py
COPY jobs/sap_ingest.py ./jobs/sap_ingest.py
COPY jobs/pricing_engine ./jobs/pricing_engine
COPY jobs/weekly_pipeline ./jobs/weekly_pipeline
ENTRYPOINT ["python", "-m", "jobs.weekly_pipeline"]
