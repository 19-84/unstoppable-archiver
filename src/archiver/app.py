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
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
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

# Shared template directory — routes/pages.py owns rendering for
# normal pages; the 404 handler in this module needs its own handle
# to render the friendly error page outside the routing layer.
_templates = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates"),
)

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
def create_app(settings: Settings | None = None) -> FastAPI:  # noqa: C901
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

    # HEAD-method shim: every @router.get route by default returns 405
    # to a HEAD request, breaking uptime monitors, HTTP cache
    # revalidators, and link checkers that preflight with HEAD before
    # GET. This middleware re-dispatches HEAD as GET so the same
    # handler runs, then strips the response body to keep the wire
    # behaviour RFC-compliant (HEAD responses must not have a body).
    @app.middleware("http")
    async def _head_as_get(request: Request, call_next):
        if request.method != "HEAD":
            return await call_next(request)
        request.scope["method"] = "GET"
        response = await call_next(request)
        if hasattr(response, "body"):
            response.body = b""
        response.headers["content-length"] = "0"
        return response

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
    #
    # The default CSP applied to every other response is pragmatic:
    # 'unsafe-inline' is permitted for both script and style because
    # the existing UI uses an inline iframe-loader script in
    # archive_view.html, two inline onsubmit confirm() handlers, and
    # an inline <style> block in base.html. The high-value clauses —
    # frame-ancestors 'none' (clickjacking), form-action 'self',
    # base-uri 'self', object-src 'none', default-src 'self' — still
    # work with inline allowed, and they block whole categories of
    # exploitation that the surrounding XSS defences can't cover (a
    # bug that injects a <script src="//attacker"> is blocked by the
    # 'self' restriction even when 'unsafe-inline' is on).
    base_csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-src 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none'"
    )

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
        h.setdefault("Content-Security-Policy", base_csp)
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

    # Status-code → user-facing heading map. 410 is omitted because
    # the takedown stub renders its own template via the route. Codes
    # not in this map fall through to JSON.
    error_headings = {
        400: "Bad request",
        401: "Sign-in required",
        403: "Forbidden",
        404: "Page not found",
        429: "Too many requests",
        500: "Something went wrong",
        502: "Upstream error",
        503: "Service unavailable",
    }

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        """Render 4xx/5xx as a friendly HTML page for browser clients
        while keeping the JSON shape for API + partials surfaces.

        Without this, every error response was a raw
        ``{"detail": "..."}`` JSON blob — fine for API consumers, but
        a confusing wall of text for a user who clicked a stale
        archive link, hit the rate limit, or accessed an admin URL
        unauthenticated. The HTML branch keeps the user oriented with
        the same Glass Noir layout, the status code, the detail
        message, and a clear link back home.

        Status codes not in _ERROR_HEADINGS fall through to JSON —
        this covers obscure 4xx like 418 / 451 / 511 where we'd
        rather not invent a heading on the fly. The 410 takedown
        stub is rendered by the route directly (it needs the archive
        object), so it never reaches this handler.
        """
        path = request.url.path
        is_api = path.startswith(("/api/", "/partials/"))
        accept = request.headers.get("accept", "")
        wants_html = "text/html" in accept or "*/*" in accept
        heading = error_headings.get(exc.status_code)
        if heading and not is_api and wants_html:
            # Retry-After is set by the rate-limit path; surface it
            # so the user knows when to try again.
            retry_after: str | None = None
            headers = getattr(exc, "headers", None) or {}
            if headers and "Retry-After" in headers:
                retry_after = str(headers["Retry-After"])
            return _templates.TemplateResponse(
                request,
                "error_404.html",
                {
                    "status_code": exc.status_code,
                    "heading": heading,
                    "detail": exc.detail,
                    "path": path,
                    "retry_after": retry_after,
                },
                status_code=exc.status_code,
                headers=headers,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None) or {},
        )

    return app
