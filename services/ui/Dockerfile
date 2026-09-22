# Cloud Run service image. Build context is the repo root:  docker build -f services/ui/Dockerfile .
#
# A pure client of diagnostic-api over DIAGNOSTIC_API_URL: no DuckDB, Postgres or SAP access, no key of its own.
FROM python:3.12-slim
WORKDIR /app
COPY services/ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY services/__init__.py ./services/__init__.py
COPY services/ui ./services/ui
# Cloud Run injects PORT; bind all interfaces, no browser to open inside the container.
ENV PORT=8501
CMD ["sh", "-c", "streamlit run services/ui/app.py --server.address 0.0.0.0 --server.port ${PORT} --server.headless true"]
