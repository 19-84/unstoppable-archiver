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
    # Camoufox (Firefox) deps
    libgtk-3-0 libx11-xcb1 \
    # Fonts for realistic fingerprint (headless browsers typically have ~1 font)
    fonts-liberation fonts-noto-core fonts-dejavu-core fontconfig \
    # Node.js for single-file-cli
    nodejs npm \
    # Camoufox Xvfb
    xvfb \
    # Utilities
    curl \
    && npm install -g single-file-cli \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.6 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# ---------------------------------------------------------------------------
# Stage: deps — install production Python dependencies
# ---------------------------------------------------------------------------
FROM base AS deps

# uv.lock must be present or --frozen can never succeed. No fallback to
# an unfrozen sync: a stale/broken lockfile should fail the build, not
# silently resolve different versions than every other environment.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Stage: dev — add dev dependencies
# ---------------------------------------------------------------------------
FROM base AS dev

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev --extra test --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev --extra test

# Install Playwright browsers (Camoufox fetched on first worker run, cached via volume)
RUN uv run playwright install chromium

EXPOSE 8000

# ---------------------------------------------------------------------------
# Stage: production — minimal runtime
# ---------------------------------------------------------------------------
FROM deps AS production

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Browsers go to a fixed path outside /root — the install runs as root
# but the runtime user is `archiver`, and Playwright's default
# /root/.cache/ms-playwright would be unreadable after USER archiver.
# (Camoufox is fetched on first run in production; mount a cache volume.)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN uv run playwright install chromium

# Non-root user for security
RUN useradd -r -m -d /home/archiver archiver && \
    mkdir -p /data/archives && chown -R archiver:archiver /data /ms-playwright

# Credentials must be passed at runtime via env vars or secrets, not baked into image
ENV ARCHIVER_ARTIFACTS_DIR=/data/archives

VOLUME ["/data"]
EXPOSE 8000

USER archiver

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uv", "run", "uvicorn", "archiver.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
