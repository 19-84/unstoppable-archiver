# ABOUTME: FastAPI dependency injection for database pool, repositories, and settings
# ABOUTME: Provides request-scoped DB connections, auth checks, and singleton repos
"""FastAPI dependency injection."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import AsyncIterator

import asyncpg.pool
from fastapi import HTTPException, Request

from archiver.blocklist import DomainBlocklist
from archiver.config import Settings
from archiver.repository import PgConnection


async def get_db(request: Request) -> AsyncIterator[PgConnection]:
    """Yield a database connection for the request lifetime."""
    pool: asyncpg.pool.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        yield conn


def get_settings(request: Request) -> Settings:
    """Return the app-wide Settings instance."""
    return request.app.state.settings  # type: ignore[no-any-return]


def get_blocklist(request: Request) -> DomainBlocklist:
    """Return the app-wide DomainBlocklist instance."""
    return request.app.state.blocklist  # type: ignore[no-any-return]


def get_client_ip_hash(request: Request) -> str:
    """Return a privacy-preserving hash of the client IP.

    Raw IPs never leave this function. Same IP produces the same hash
    (for abuse correlation) but the hash is non-reversible with the salt.
    """
    settings: Settings = request.app.state.settings
    raw = request.client.host if request.client else ""
    if settings.trusted_proxies:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            raw = xff.split(",")[0].strip()
    if not raw:
        return ""
    salt = (
        settings.ip_hash_salt.get_secret_value()
        or settings.session_secret.get_secret_value()
    )
    return hmac.new(
        salt.encode(), raw.encode(), hashlib.sha256
    ).hexdigest()[:32]


async def require_api_key(request: Request) -> None:
    """Check API key for destructive operations.

    If ARCHIVER_API_KEY is not set (empty), auth is disabled.
    If set, the request must include it as Bearer token or X-API-Key header.

    Comparisons go through hmac.compare_digest so a network attacker
    can't byte-by-byte recover the key via response-timing
    measurements — plain ``==`` on strings short-circuits at the first
    differing byte, leaking how many leading bytes were correct.
    """
    import hmac

    settings = request.app.state.settings
    key = settings.api_key.get_secret_value()
    if not key:
        return  # Auth disabled

    # Check Authorization header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], key):
        return

    # Check X-API-Key header
    xapi = request.headers.get("x-api-key", "")
    if xapi and hmac.compare_digest(xapi, key):
        return

    raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def require_metrics_token(request: Request) -> None:
    """Gate the /metrics endpoint with a bearer token.

    The API process serves /metrics on the public :8000 (the worker's
    metrics port is localhost-only). If ARCHIVER_METRICS_TOKEN is set,
    a scraper must send `Authorization: Bearer <token>`; empty leaves
    the endpoint open. Only Bearer is accepted — Prometheus' scrape
    `authorization` config sends exactly that, and the endpoint has no
    human callers, so the X-API-Key path require_api_key allows is
    deliberately not mirrored. hmac.compare_digest avoids leaking the
    token via response-timing.
    """
    settings = request.app.state.settings
    token = settings.metrics_token.get_secret_value()
    if not token:
        return  # Auth disabled

    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], token):
        return

    raise HTTPException(
        status_code=401, detail="Invalid or missing metrics token"
    )
