# ABOUTME: PostgreSQL connection pool and schema initialization via asyncpg
# ABOUTME: Creates archives + jobs tables with tsvector FTS, GIN index, and partial indexes
"""PostgreSQL database initialization and connection pool management."""

from __future__ import annotations

import asyncpg
import structlog
from beartype import beartype

log = structlog.get_logger()

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS archives (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    title TEXT,
    text_content TEXT,
    search_vector tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(text_content, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(url, '')), 'C')
    ) STORED,
    status TEXT NOT NULL DEFAULT 'pending',
    tier TEXT NOT NULL DEFAULT 'chromium',
    source TEXT NOT NULL DEFAULT 'direct',
    error_message TEXT,
    artifact_dir TEXT,
    content_hash TEXT,
    screenshot_hash TEXT,
    revisit_of TEXT REFERENCES archives(id),
    snapshot_size BIGINT,
    warc_size BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    metadata JSONB
);

CREATE INDEX IF NOT EXISTS idx_archives_url_hash ON archives(url_hash);
CREATE INDEX IF NOT EXISTS idx_archives_status ON archives(status);
CREATE INDEX IF NOT EXISTS idx_archives_created_at ON archives(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_archives_content_hash ON archives(content_hash);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_archives_search'
    ) THEN
        CREATE INDEX idx_archives_search ON archives USING GIN(search_vector);
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    archive_id TEXT NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    tier TEXT NOT NULL DEFAULT 'chromium',
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    locked_by TEXT,
    locked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_jobs_archive_id ON jobs(archive_id);
"""

# Partial index for claimable jobs — needs special handling
PARTIAL_INDEX_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes WHERE indexname = 'idx_jobs_claimable'
    ) THEN
        CREATE INDEX idx_jobs_claimable ON jobs(priority DESC, created_at)
            WHERE status = 'queued';
    END IF;
END$$;
"""


@beartype
async def create_pool(db_url: str, *, min_size: int = 2, max_size: int = 10) -> asyncpg.Pool:
    """Create and return a connection pool."""
    pool = await asyncpg.create_pool(
        db_url, min_size=min_size, max_size=max_size
    )
    assert pool is not None  # noqa: S101
    log.info("db.pool_created", min_size=min_size, max_size=max_size)
    return pool


@beartype
async def init_db(pool: asyncpg.Pool) -> None:
    """Create database schema if it doesn't exist."""
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        await conn.execute(PARTIAL_INDEX_SQL)
    log.info("db.schema_initialized")


@beartype
async def close_pool(pool: asyncpg.Pool) -> None:
    """Close the connection pool."""
    await pool.close()
    log.info("db.pool_closed")
