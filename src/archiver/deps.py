# ABOUTME: FastAPI dependency injection for database pool, repositories, and settings
# ABOUTME: Provides request-scoped DB connections, auth checks, and singleton repos
"""FastAPI dependency injection."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg.pool
from fastapi import HTTPException, Request

from archiver.repository import PgConnection


async def get_db(request: Request) -> AsyncIterator[PgConnection]:
    """Yield a database connection for the request lifetime."""
    pool: asyncpg.pool.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        yield conn


async def require_api_key(request: Request) -> None:
    """Check API key for destructive operations.

    If ARCHIVER_API_KEY is not set (empty), auth is disabled.
    If set, the request must include it as Bearer token or X-API-Key header.
    """
    settings = request.app.state.settings
    key = settings.api_key.get_secret_value()
    if not key:
        return  # Auth disabled

    # Check Authorization header
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == key:
        return

    # Check X-API-Key header
    if request.headers.get("x-api-key") == key:
        return

    raise HTTPException(status_code=401, detail="Invalid or missing API key")
