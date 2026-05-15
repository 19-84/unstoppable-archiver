# ABOUTME: FastAPI application factory with lifespan management
# ABOUTME: Wires routes, DB pool, settings, middleware, and exception handlers into the ASGI app
# pyright: reportUnusedFunction=false
"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from beartype import beartype
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from archiver.blocklist import load_blocklist
from archiver.config import Settings
from archiver.db import close_pool, create_pool, init_db
from archiver.errors import DuplicateCaptureError
from archiver.logging import setup_logging
from archiver.routes.admin import router as admin_router
from archiver.routes.archives import router as archives_router
from archiver.routes.health import router as health_router
from archiver.routes.pages import router as pages_router
from archiver.routes.reports import router as reports_router

log = structlog.get_logger()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover
    """Manage DB pool + blocklist lifecycle."""
    settings: Settings = app.state.settings
    pool = await create_pool(settings.db_url.get_secret_value(), min_size=2, max_size=10)
    await init_db(pool)
    app.state.pool = pool
    app.state.blocklist = await load_blocklist(settings)
    log.info(
        "app.started",
        mode=settings.mode,
        blocked=app.state.blocklist.blocked_count,
        allowed=app.state.blocklist.allowed_count,
    )
    yield
    await close_pool(pool)
    log.info("app.stopped")


@beartype
def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = Settings()

    setup_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Unstoppable Archive",
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.settings = settings

    # Session middleware (for admin auth) — always installed but only used
    # when admin_enabled. https_only=True in public mode.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        https_only=(settings.mode == "public"),
        same_site="lax",
    )

    # App-wide security headers. The Caddy reverse proxy in the public
    # profile already sets these, but self-hosted operators who put
    # nothing in front of the app (a tailnet, an internal network)
    # were missing them. Setting them at the app layer is harmless when
    # a proxy is also setting them — the proxy's headers win since they
    # ship the final response, but ours apply during direct access.
    #
    # CSP is deliberately permissive on default-src because the app
    # serves htmx, vendored CSS, and archived snapshots; a strict CSP
    # at this layer would break those. The snapshot serve path sets
    # its own per-response `Content-Security-Policy: sandbox` so
    # untrusted captured HTML still can't run script in our origin.
    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        # Skip on the snapshot route — it sets its own CSP-sandbox and
        # we don't want to clobber it with a less-restrictive header.
        if request.url.path.endswith("/snapshot"):
            return response
        h = response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        h.setdefault("X-Frame-Options", "SAMEORIGIN")
        h.setdefault("Permissions-Policy", "interest-cohort=()")
        # HSTS only meaningful over HTTPS; setting it on plain-HTTP
        # responses is fine (browsers ignore it). Public mode is always
        # behind Caddy which terminates TLS.
        if settings.mode == "public":
            h.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response

    # Static files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Routes. Health router is mounted at both /api (legacy) and root so
    # /metrics matches Prometheus scrape defaults without needing a
    # metrics_path override in prometheus.yml.
    app.include_router(health_router, prefix="/api")
    app.include_router(health_router)
    app.include_router(archives_router, prefix="/api")
    app.include_router(reports_router)
    app.include_router(admin_router)
    app.include_router(pages_router)

    # Exception handlers. Only the duplicate-capture path raises a
    # custom AppError that reaches the API surface — capture / storage
    # / proxy errors live inside the worker and never propagate here,
    # and route 404s use HTTPException directly. The other handlers
    # this file used to register (NotFoundError, AppError catch-all)
    # were dead and removed; add them back when a route actually needs
    # one.
    @app.exception_handler(DuplicateCaptureError)
    async def _duplicate_handler(
        request: Request, exc: DuplicateCaptureError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": exc.code,
                "message": exc.message,
                "existing_id": exc.existing_id,
            },
        )

    return app
