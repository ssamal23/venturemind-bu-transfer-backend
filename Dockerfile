# One image, three jobs. The command decides which:
#   API      uvicorn app.main:app --host 0.0.0.0 --port 8000        (the default)
#   Worker   python -m app.workers.evaluation_worker
#   Setup    alembic upgrade head / python -m app.db.seed ...       (run once, as a job)
#
# Build:  docker build -t bu-api .        or, with no local Docker:  az acr build ...  (see docs/deployment.md)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/opt/models \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the embedding model into the image (about 90 MB), so a starting container never downloads it.
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app
COPY data ./data
COPY scripts ./scripts

# Run as an ordinary user, not root.
RUN useradd --create-home --uid 10001 app && chown -R app /app /opt/models
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
