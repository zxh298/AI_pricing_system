# Cloud Run job image. Build context is the repo root:  docker build -f jobs/weekly_triage/Dockerfile .
#
# Holds the SAP read key and the LLM key, never SAP_WRITE_KEY, and mounts the warehouse read-only: like diagnostic-api
# it can look at everything and change no price.
FROM python:3.12-slim
WORKDIR /app
COPY jobs/weekly_triage/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY jobs/__init__.py ./jobs/__init__.py
COPY jobs/weekly_pipeline/__init__.py ./jobs/weekly_pipeline/__init__.py
COPY jobs/weekly_pipeline/reconcile.py ./jobs/weekly_pipeline/reconcile.py
COPY jobs/weekly_pipeline/validate.py ./jobs/weekly_pipeline/validate.py
COPY jobs/weekly_triage ./jobs/weekly_triage
COPY services/__init__.py ./services/__init__.py
COPY services/diagnostic_api ./services/diagnostic_api
ENTRYPOINT ["python", "-m", "jobs.weekly_triage"]
