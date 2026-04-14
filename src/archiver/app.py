# ABOUTME: FastAPI application factory with lifespan management
# ABOUTME: Wires routes, DB pool, settings, and exception handlers into the ASGI app
# pyright: reportUnusedFunction=false
"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from beartype import beartype
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from archiver.config import Settings
from archiver.db import close_pool, create_pool, init_db
from archiver.errors import AppError, DuplicateCaptureError, NotFoundError
from archiver.logging import setup_logging
from archiver.routes.archives import router as archives_router
from archiver.routes.health import router as health_router

log = structlog.get_logger()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage DB pool lifecycle."""
    settings: Settings = app.state.settings
    pool = await create_pool(settings.db_url, min_size=2, max_size=10)
    await init_db(pool)
    app.state.pool = pool
    log.info("app.started")
    yield
    await close_pool(pool)
    log.info("app.stopped")


_ERROR_STATUS_MAP: dict[str, int] = {
    "NOT_FOUND": 404,
    "DUPLICATE_CAPTURE": 409,
    "CAPTURE_ERROR": 502,
    "ANTI_BOT_DETECTED": 502,
    "STORAGE_ERROR": 500,
    "PROXY_UNAVAILABLE": 503,
    "UNKNOWN": 500,
}


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

    # Routes
    app.include_router(health_router, prefix="/api")
    app.include_router(archives_router, prefix="/api")

    # Exception handlers
    @app.exception_handler(NotFoundError)
    async def _not_found_handler(
        request: Request, exc: NotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"error": exc.code, "message": exc.message},
        )

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

    @app.exception_handler(AppError)
    async def _app_error_handler(
        request: Request, exc: AppError
    ) -> JSONResponse:
        status = _ERROR_STATUS_MAP.get(exc.code, 500)
        return JSONResponse(
            status_code=status,
            content={"error": exc.code, "message": exc.message},
        )

    return app
