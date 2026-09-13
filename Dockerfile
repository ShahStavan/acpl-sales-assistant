# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install the package first so source edits do not invalidate the dependency layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Absolute paths, set BEFORE preparation. Settings derive their defaults from the package's
# own location, which is site-packages once installed rather than a source checkout, so a
# relative value resolves against the interpreter's lib directory instead of /app. An
# absolute value wins that join, and is what makes the data pack and the warehouse both
# resolve correctly inside the image — at build time and at serve time alike.
ENV PORT=8000 \
    ACPL_DATA_DIR=/app/data/fmcg-sales-copilot-ai-engineer-mid-4to6 \
    ACPL_WAREHOUSE=/app/warehouse.duckdb

# Data pack (read-only input) and the one-command preparation.
COPY data ./data
COPY prepare.py ./
RUN python prepare.py

# Non-root runtime user; the warehouse is read-only at serve time.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health')" || exit 1

CMD ["sh", "-c", "uvicorn acpl_assistant.service:app --host 0.0.0.0 --port ${PORT}"]
