# Cloud Run service image. Build context is the repo root:  docker build -f services/diagnostic_api/Dockerfile .
#
# Holds only the SAP read key and the LLM key -- never SAP_WRITE_KEY (that stays with weekly-pipeline's image and
# service account only, so this service has no way to change a price). The DuckDB warehouse is mounted read-only.
FROM python:3.12-slim
WORKDIR /app
COPY services/diagnostic_api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
# validate_rows and reconcile are pure functions reused from the pipeline (docs/PROJECT_CONTEXT.md section 17.8),
# so the pipeline and the diagnostic assistant can never disagree about what counts as a violation.
COPY jobs/__init__.py ./jobs/__init__.py
COPY jobs/weekly_pipeline/__init__.py ./jobs/weekly_pipeline/__init__.py
COPY jobs/weekly_pipeline/reconcile.py ./jobs/weekly_pipeline/reconcile.py
COPY jobs/weekly_pipeline/validate.py ./jobs/weekly_pipeline/validate.py
COPY services/__init__.py ./services/__init__.py
COPY services/diagnostic_api ./services/diagnostic_api
# The knowledge base itself is ingested by a separate step (python -m jobs.rag_ingest, not containerized yet);
# this image only reads it, so playbooks/ is not copied in.
# Cloud Run injects PORT; bind all interfaces.
ENV PORT=8000
CMD ["sh", "-c", "uvicorn services.diagnostic_api.app:app --host 0.0.0.0 --port ${PORT}"]
