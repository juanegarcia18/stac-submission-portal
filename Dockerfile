FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NACC_HOST=0.0.0.0 \
    NACC_PORT=8000 \
    NACC_JOB_ROOT=/var/lib/nacc-pipeline/jobs \
    NACC_RUNTIME_ROOT=/var/lib/nacc-pipeline/runtime \
    NACC_DEPLOYMENT_ENV=production

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --shell /usr/sbin/nologin naccapp \
    && mkdir -p /var/lib/nacc-pipeline/jobs /var/lib/nacc-pipeline/runtime \
    && chown -R naccapp:naccapp /var/lib/nacc-pipeline /app

USER naccapp

EXPOSE 8000
VOLUME ["/var/lib/nacc-pipeline"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json, urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)); raise SystemExit(0 if data.get('status') == 'ok' else 1)"

CMD ["python", "-m", "backend.serve_flask_app"]
