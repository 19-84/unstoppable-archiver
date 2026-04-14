# ABOUTME: Health check endpoints for liveness and readiness probes
# ABOUTME: Shallow health returns OK; deep health verifies PostgreSQL connectivity
"""Health check routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from archiver.deps import get_db
from archiver.repository import PgConnection

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Shallow health check — app is running."""
    return {"status": "ok"}


@router.get("/health/deep")
async def health_deep(
    conn: Annotated[PgConnection, Depends(get_db)],
) -> dict[str, str]:
    """Deep health check — verifies PostgreSQL connectivity."""
    await conn.fetchval("SELECT 1")
    return {"status": "ok", "database": "connected"}
