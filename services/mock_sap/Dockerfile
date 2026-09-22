# Build context is the repo root so shared/ is included:  docker build -f services/mock_sap/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY services/mock_sap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY services/__init__.py ./services/__init__.py
COPY services/mock_sap ./services/mock_sap
# Cloud Run injects PORT; bind all interfaces.
ENV PORT=8001
CMD ["sh", "-c", "uvicorn services.mock_sap.main:app --host 0.0.0.0 --port ${PORT}"]
