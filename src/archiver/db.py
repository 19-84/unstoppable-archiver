# ABOUTME: PostgreSQL connection pool and schema initialization via asyncpg
# ABOUTME: Creates archives + jobs tables with tsvector FTS, GIN index, and partial indexes
"""PostgreSQL database initialization and connection pool management."""

from __future__ import annotations

import asyncpg
import structlog
from beartype import beartype

log = structlog.get_logger()

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

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
    metadata JSONB,
    removed_at TIMESTAMPTZ,
    removed_reason TEXT,
    submitter_ip_hash TEXT
);

-- Add columns to existing databases (idempotent)
ALTER TABLE archives ADD COLUMN IF NOT EXISTS removed_at TIMESTAMPTZ;
ALTER TABLE archives ADD COLUMN IF NOT EXISTS removed_reason TEXT;
ALTER TABLE archives ADD COLUMN IF NOT EXISTS submitter_ip_hash TEXT;
-- Drop old raw-IP columns if upgrading from an earlier version (privacy fix)
ALTER TABLE archives DROP COLUMN IF EXISTS submitter_ip;
ALTER TABLE archives DROP COLUMN IF EXISTS submitter_ip_hashed;

CREATE INDEX IF NOT EXISTS idx_archives_url_hash ON archives(url_hash);
CREATE INDEX IF NOT EXISTS idx_archives_status ON archives(status);
CREATE INDEX IF NOT EXISTS idx_archives_created_at ON archives(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_archives_content_hash ON archives(content_hash);
CREATE INDEX IF NOT EXISTS idx_archives_removed_at ON archives(removed_at);

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

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    action TEXT NOT NULL,
    archive_id TEXT REFERENCES archives(id) ON DELETE SET NULL,
    admin_user TEXT,
    ip_address_hash TEXT,
    details JSONB
);
-- Rename on upgrade if coming from earlier schema (idempotent)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='audit_log' AND column_name='ip_address')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='audit_log' AND column_name='ip_address_hash') THEN
        ALTER TABLE audit_log RENAME COLUMN ip_address TO ip_address_hash;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_archive_id ON audit_log(archive_id);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    archive_id TEXT NOT NULL REFERENCES archives(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    details TEXT,
    reporter_email TEXT,
    reporter_ip_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT,
    resolution_notes TEXT
);
-- Rename on upgrade (idempotent)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='reports' AND column_name='reporter_ip')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='reports' AND column_name='reporter_ip_hash') THEN
        ALTER TABLE reports RENAME COLUMN reporter_ip TO reporter_ip_hash;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
CREATE INDEX IF NOT EXISTS idx_reports_archive_id ON reports(archive_id);
CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports(created_at DESC);

-- Per-proxy gate-pass status. Separate from the live ProxyRotator's
-- in-memory failure set so we can prefer known-good exits across
-- worker restarts.
CREATE TABLE IF NOT EXISTS proxy_status (
    proxy_server TEXT PRIMARY KEY,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    gate_passing BOOLEAN NOT NULL DEFAULT false,
    asn_org TEXT,
    country_code TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_proxy_status_passing
    ON proxy_status(last_checked_at DESC) WHERE gate_passing = true;

-- Persistent cookie cache keyed by (domain, proxy). Empty proxy_server
-- means direct connection. Stores solved cf_clearance so the next
-- capture through the same (domain, proxy) skips the challenge.
CREATE TABLE IF NOT EXISTS cf_clearance_cache (
    domain TEXT NOT NULL,
    proxy_server TEXT NOT NULL DEFAULT '',
    cookie_name TEXT NOT NULL,
    cookie_value TEXT NOT NULL,
    cookie_path TEXT NOT NULL DEFAULT '/',
    expires_at TIMESTAMPTZ NOT NULL,
    set_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (domain, proxy_server, cookie_name)
);
CREATE INDEX IF NOT EXISTS idx_cf_clearance_expires
    ON cf_clearance_cache(expires_at);
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
