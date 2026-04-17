# ABOUTME: Health check endpoints for liveness and readiness probes
# ABOUTME: Shallow health returns OK; deep health verifies PostgreSQL connectivity
"""Health check routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from archiver.deps import get_db
from archiver.metrics import jobs_queued, prometheus_text
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


@router.get("/metrics", include_in_schema=False)
async def metrics(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> Response:
    """Prometheus metrics endpoint (text/plain)."""
    # Refresh real-time gauges before serialization
    queued = await conn.fetchval(
        "SELECT count(*) FROM jobs WHERE status = 'queued'"
    )
    jobs_queued.set(queued or 0)
    body, content_type = prometheus_text()
    return Response(content=body, media_type=content_type)
