# ABOUTME: FastAPI dependency injection for database pool, repositories, and settings
# ABOUTME: Provides request-scoped DB connections and singleton repos to route handlers
"""FastAPI dependency injection."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg.pool
from fastapi import Request

from archiver.repository import PgConnection


async def get_db(request: Request) -> AsyncIterator[PgConnection]:
    """Yield a database connection for the request lifetime."""
    pool: asyncpg.pool.Pool = request.app.state.pool
    async with pool.acquire() as conn:
        yield conn
