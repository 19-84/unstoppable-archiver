# ABOUTME: Integration tests for PostgreSQL schema initialization
# ABOUTME: Verifies table creation, idempotency, FTS vector column, and GIN index
"""Tests for database initialization and schema."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest

from archiver.db import close_pool, create_pool, init_db

DB_URL = os.environ.get(
    "ARCHIVER_TEST_DB_URL",
    "postgresql://archiver:archiver@localhost:15432/archiver_test",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.pool.Pool]:
    """Pool with a clean schema BEFORE each test.

    test_db.py exercises init_db itself, so it needs to start from a
    state where the tables don't exist. Clean-slate setup (DROP +
    clear schema_migrations) instead of clean-slate teardown — that
    way after each test runs, init_db has just rebuilt the schema and
    the DB is left in a healthy state for any other integration test
    that happens to run next (pytest-randomly shuffles file order).
    """
    p = await create_pool(DB_URL, min_size=1, max_size=2)
    async with p.acquire() as conn:
        # Drop every table holding an FK to archives, not just jobs:
        # `DROP ... archives CASCADE` silently strips the FK constraint
        # from surviving tables, and init_db's CREATE TABLE IF NOT
        # EXISTS never restores it — leaving the test schema laxer than
        # production for the rest of the session (this hid a real FK
        # violation in the hard-delete route).
        await conn.execute("DROP TABLE IF EXISTS audit_log CASCADE")
        await conn.execute("DROP TABLE IF EXISTS reports CASCADE")
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS archives CASCADE")
        await conn.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
    yield p
    await close_pool(p)


class TestInitDb:
    async def test_creates_tables(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        await init_db(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT EXISTS ("
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'archives'"
                ")"
            )
            assert row is not None
            assert row[0] is True

            row = await conn.fetchrow(
                "SELECT EXISTS ("
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'jobs'"
                ")"
            )
            assert row is not None
            assert row[0] is True

    async def test_idempotent(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        await init_db(pool)
        await init_db(pool)

    async def test_archives_has_search_vector(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        await init_db(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT column_name "
                "FROM information_schema.columns "
                "WHERE table_name = 'archives' "
                "AND column_name = 'search_vector'"
            )
            assert row is not None

    async def test_gin_index_exists(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        await init_db(pool)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM pg_indexes "
                "WHERE indexname = 'idx_archives_search'"
            )
            assert row is not None
