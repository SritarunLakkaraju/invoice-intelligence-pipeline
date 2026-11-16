FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 CREWAI_DISABLE_TELEMETRY=true OTEL_SDK_DISABLED=true
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python -m fde4_doc_pipeline.generate_invoices
RUN useradd -m app && chown -R app /app
USER app
CMD exec uvicorn fde4_doc_pipeline.api:app --host 0.0.0.0 --port ${PORT}
