# Task Rework Dashboard (spec 14). Run a single instance only: SQLite can't be shared by replicas.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    REQUIRE_PASSWORD=true

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN mkdir -p /data

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8501\")}/_stcore/health')" || exit 1

CMD streamlit run app.py --server.port ${PORT:-8501} --server.address 0.0.0.0 --server.headless true
