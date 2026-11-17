# Beacon audit API — Python + headless Chromium (needed for the site-fetch fallback in
# tools/analyze_site.py, which is why this runs on a container host, not serverless).
FROM python:3.12-slim

WORKDIR /app

# Python deps first for layer caching, then the Chromium browser + its OS libraries.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium

# App code only (see .dockerignore — .env, web/, .tmp/, media, git are excluded).
COPY tools ./tools
COPY api ./api
COPY templates ./templates
COPY assets ./assets

ENV AUDIT_SYNTH_BACKEND=groq \
    BRAND_NAME=Beacon \
    PYTHONUNBUFFERED=1

# SINGLE worker on purpose: the job registry (api/jobs.py) is in-process/in-memory, so
# /status must hit the same process that ran the job. Scale-up = Redis + Arq + N workers.
# $PORT is provided by the host (Render); default 8000 for local `docker run`.
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
