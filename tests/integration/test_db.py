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
    "ARCHIVER_DB_URL",
    "postgresql://archiver:archiver@localhost:15432/archiver",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.pool.Pool]:
    p = await create_pool(DB_URL, min_size=1, max_size=2)
    yield p
    async with p.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS archives CASCADE")
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
