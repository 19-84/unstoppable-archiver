# ABOUTME: Integration tests for ArchiveRepository and JobRepository
# ABOUTME: Tests CRUD, FTS search, job claiming, dedup, and NOTIFY against real PostgreSQL
"""Tests for repository layer against real PostgreSQL."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest

from archiver.db import close_pool, create_pool, init_db
from archiver.enums import ArchiveStatus, CaptureTier, JobStatus
from archiver.repository import ArchiveRepository, JobRepository
from archiver.url import url_hash

DB_URL = os.environ.get(
    "ARCHIVER_DB_URL",
    "postgresql://archiver:archiver@localhost:15432/archiver",
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.pool.Pool]:
    p = await create_pool(DB_URL, min_size=2, max_size=5)
    await init_db(p)
    yield p
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM jobs")
        await conn.execute("DELETE FROM archives")
    await close_pool(p)


@pytest.fixture
def archive_repo() -> ArchiveRepository:
    return ArchiveRepository()


@pytest.fixture
def job_repo() -> JobRepository:
    return JobRepository()


class TestArchiveRepository:
    async def test_create_and_get(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com/page"
            )
            assert archive.status == ArchiveStatus.PENDING
            assert archive.tier == CaptureTier.CHROMIUM
            assert archive.url_hash == url_hash(
                "https://example.com/page"
            )

            fetched = await archive_repo.get_by_id(conn, archive.id)
            assert fetched is not None
            assert fetched.id == archive.id
            assert fetched.url == archive.url

    async def test_get_nonexistent_returns_none(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            result = await archive_repo.get_by_id(conn, "nonexistent")
            assert result is None

    async def test_get_by_url_hash(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            await archive_repo.create(conn, "https://example.com")
            await archive_repo.create(conn, "https://example.com")
            uhash = url_hash("https://example.com")
            results = await archive_repo.get_by_url_hash(conn, uhash)
            assert len(results) == 2  # noqa: PLR2004

    async def test_update_status(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            updated = await archive_repo.update_status(
                conn,
                archive.id,
                ArchiveStatus.COMPLETE,
                title="Example",
                text_content="Hello world",
            )
            assert updated is not None
            assert updated.status == ArchiveStatus.COMPLETE
            assert updated.title == "Example"
            assert updated.text_content == "Hello world"

    async def test_search_fts(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            await archive_repo.update_status(
                conn,
                archive.id,
                ArchiveStatus.COMPLETE,
                title="Python Tutorial",
                text_content="Learn Python programming language",
            )

            results = await archive_repo.search(conn, "python")
            assert results.total >= 1
            assert any(
                a.id == archive.id for a in results.archives
            )

    async def test_search_returns_empty(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            results = await archive_repo.search(
                conn, "xyznonexistent999"
            )
            assert results.total == 0
            assert results.archives == []

    async def test_check_recent_capture(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            await archive_repo.update_status(
                conn,
                archive.id,
                ArchiveStatus.COMPLETE,
            )
            # Should find it (captured just now, interval=3600)
            recent = await archive_repo.check_recent_capture(
                conn, archive.url_hash, 3600
            )
            assert recent is not None
            assert recent.id == archive.id

            # Should NOT find with interval=0
            none_result = await archive_repo.check_recent_capture(
                conn, archive.url_hash, 0
            )
            assert none_result is None

    async def test_create_revisit(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            original = await archive_repo.create(
                conn, "https://example.com"
            )
            await archive_repo.update_status(
                conn, original.id, ArchiveStatus.COMPLETE
            )

            dup = await archive_repo.create(
                conn, "https://example.com"
            )
            revisit = await archive_repo.create_revisit(
                conn, dup.id, original.id, "abc123hash"
            )
            assert revisit is not None
            assert revisit.revisit_of == original.id
            assert revisit.content_hash == "abc123hash"
            assert revisit.status == ArchiveStatus.COMPLETE

    async def test_list_recent(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            await archive_repo.create(conn, "https://a.com")
            await archive_repo.create(conn, "https://b.com")
            await archive_repo.create(conn, "https://c.com")

            archives, total = await archive_repo.list_recent(
                conn, limit=2
            )
            assert len(archives) == 2  # noqa: PLR2004
            assert total == 3  # noqa: PLR2004

    async def test_delete(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            deleted = await archive_repo.delete(conn, archive.id)
            assert deleted is True

            fetched = await archive_repo.get_by_id(conn, archive.id)
            assert fetched is None

    async def test_delete_nonexistent(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            deleted = await archive_repo.delete(conn, "nonexistent")
            assert deleted is False


class TestJobRepository:
    async def test_enqueue_and_claim(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            job = await job_repo.enqueue(
                conn, archive.id, CaptureTier.CHROMIUM
            )
            assert job.status == JobStatus.QUEUED
            assert job.archive_id == archive.id

            claimed = await job_repo.claim_next(conn, "worker-1")
            assert claimed is not None
            assert claimed.id == job.id
            assert claimed.status == JobStatus.RUNNING
            assert claimed.locked_by == "worker-1"
            assert claimed.attempts == 1

    async def test_claim_empty_queue(
        self,
        pool: asyncpg.pool.Pool,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            claimed = await job_repo.claim_next(conn, "worker-1")
            assert claimed is None

    async def test_claim_respects_priority(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            a1 = await archive_repo.create(conn, "https://low.com")
            a2 = await archive_repo.create(conn, "https://high.com")

            await job_repo.enqueue(
                conn, a1.id, CaptureTier.CHROMIUM, priority=0
            )
            await job_repo.enqueue(
                conn, a2.id, CaptureTier.CHROMIUM, priority=10
            )

            claimed = await job_repo.claim_next(conn, "worker-1")
            assert claimed is not None
            assert claimed.archive_id == a2.id

    async def test_complete_job(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            job = await job_repo.enqueue(
                conn, archive.id, CaptureTier.CHROMIUM
            )
            await job_repo.claim_next(conn, "worker-1")

            completed = await job_repo.complete(conn, job.id)
            assert completed is not None
            assert completed.status == JobStatus.COMPLETE
            assert completed.completed_at is not None

    async def test_fail_without_retry(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            job = await job_repo.enqueue(
                conn, archive.id, CaptureTier.CHROMIUM
            )
            await job_repo.claim_next(conn, "worker-1")

            failed = await job_repo.fail(
                conn, job.id, "timeout", retry=False
            )
            assert failed is not None
            assert failed.status == JobStatus.FAILED
            assert failed.error_message == "timeout"

    async def test_fail_with_retry_creates_new_job(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            job = await job_repo.enqueue(
                conn, archive.id, CaptureTier.CHROMIUM
            )
            await job_repo.claim_next(conn, "worker-1")

            await job_repo.fail(
                conn, job.id, "transient error", retry=True
            )

            # A new queued job should exist
            new_job = await job_repo.claim_next(conn, "worker-2")
            assert new_job is not None
            assert new_job.id != job.id
            assert new_job.archive_id == archive.id

    async def test_reclaim_stale(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            await job_repo.enqueue(
                conn, archive.id, CaptureTier.CHROMIUM
            )
            await job_repo.claim_next(conn, "worker-1")

            # Backdate the lock to simulate staleness
            await conn.execute(
                "UPDATE jobs SET locked_at = now() - interval '10 minutes'"
                " WHERE locked_by = 'worker-1'"
            )

            reclaimed = await job_repo.reclaim_stale(conn, 300)
            assert reclaimed == 1

            # Should be claimable again
            job = await job_repo.claim_next(conn, "worker-2")
            assert job is not None
            assert job.locked_by == "worker-2"

    async def test_get_by_id(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            job = await job_repo.enqueue(
                conn, archive.id, CaptureTier.CHROMIUM
            )

            fetched = await job_repo.get_by_id(conn, job.id)
            assert fetched is not None
            assert fetched.id == job.id

    async def test_cascade_delete(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            job = await job_repo.enqueue(
                conn, archive.id, CaptureTier.CHROMIUM
            )

            await archive_repo.delete(conn, archive.id)

            fetched_job = await job_repo.get_by_id(conn, job.id)
            assert fetched_job is None
