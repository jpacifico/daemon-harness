FROM python:3.11.15-slim

ARG DAEMON_CORE_RUNTIME_REVISION=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src \
    DAEMON_CORE_RUNTIME_REVISION=${DAEMON_CORE_RUNTIME_REVISION}

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import json, urllib.request; report=json.load(urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)); assert report.get('promotable') is True"]
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]
