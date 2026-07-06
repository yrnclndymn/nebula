# Nebula backend for Cloud Run. Build context: backend/
# Deploy: gcloud run deploy nebula-api --source backend ...
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Install dependencies first (cached layer); pyproject has [tool.uv] package=false,
# so this installs deps only (no project build, so no README needed).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App code
COPY app ./app

# Cloud Run provides $PORT (default 8080). Shell form so $PORT expands.
EXPOSE 8080
CMD uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
