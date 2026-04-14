# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage: base — Python + system deps for Playwright/Xvfb
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    # Playwright Chromium deps
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2t64 libxshmfence1 \
    # Camoufox Xvfb
    xvfb \
    # Utilities
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# ---------------------------------------------------------------------------
# Stage: deps — install production Python dependencies
# ---------------------------------------------------------------------------
FROM base AS deps

COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project 2>/dev/null || \
    uv sync --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Stage: dev — add dev dependencies
# ---------------------------------------------------------------------------
FROM base AS dev

COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev --extra test --no-install-project 2>/dev/null || \
    uv sync --extra dev --extra test --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev --extra test 2>/dev/null || uv sync --extra dev --extra test

# Install Playwright browsers
RUN uv run playwright install chromium

EXPOSE 8000

# ---------------------------------------------------------------------------
# Stage: test — run quality gates
# ---------------------------------------------------------------------------
FROM dev AS test

RUN uv run ruff check .
RUN uv run pyright
RUN uv run pytest --tb=short -q -m "not slow"

# ---------------------------------------------------------------------------
# Stage: production — minimal runtime
# ---------------------------------------------------------------------------
FROM deps AS production

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# Install browsers
RUN uv run playwright install chromium

ENV ARCHIVER_DB_URL=postgresql://archiver:archiver@postgres:5432/archiver \
    ARCHIVER_ARTIFACTS_DIR=/data/archives

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1
