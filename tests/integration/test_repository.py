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
from archiver.repository import (
    ArchiveRepository,
    CfClearanceRepository,
    DomainObservationsRepository,
    FrontendStatusRepository,
    JobRepository,
    ProxyStatusRepository,
)
from archiver.url import url_hash
from tests.integration.conftest import reset_test_db

DB_URL = os.environ.get(
    "ARCHIVER_TEST_DB_URL",
    "postgresql://archiver:archiver@localhost:15432/archiver_test",
)

# Module-level safety guard lives in tests/integration/conftest.py
# now (applies to every integration test, not just this file).

pytestmark = pytest.mark.integration


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.pool.Pool]:
    p = await create_pool(DB_URL, min_size=2, max_size=5)
    await init_db(p)
    await reset_test_db(p)   # clean slate IN, not OUT
    yield p
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

    async def test_update_status_with_metadata(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        """metadata JSONB round-trips through update_status + get_by_id.

        The privacy_frontend / wayback / archive_today / commoncrawl
        capture paths write source_url into metadata so the UI can
        show 'captured from <instance>' without losing the original
        submission URL. Verify the column is wired end-to-end.
        """
        import json
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://twitter.com/jack/status/20",
            )
            meta = {
                "source_url": "https://nitter.tiekoetter.com/jack/status/20",
            }
            updated = await archive_repo.update_status(
                conn,
                archive.id,
                ArchiveStatus.COMPLETE,
                metadata=json.dumps(meta),
            )
            assert updated is not None
            assert updated.metadata == meta
            # Round-trip via get_by_id too (different code path: that
            # exercises _record_to_archive's JSONB decode, not the
            # UPDATE ... RETURNING path).
            fetched = await archive_repo.get_by_id(conn, archive.id)
            assert fetched is not None
            assert fetched.metadata == meta

    async def test_update_status_metadata_none_stays_none(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        """Direct-capture tiers leave source_url=None so the worker
        skips passing metadata. The column stays NULL → decode yields
        None, not an empty dict."""
        async with pool.acquire() as conn:
            archive = await archive_repo.create(conn, "https://e.com/")
            updated = await archive_repo.update_status(
                conn, archive.id, ArchiveStatus.COMPLETE,
            )
            assert updated is not None
            assert updated.metadata is None

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

    async def test_fail_with_retry_at_job_cap_marks_archive_failed(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
        job_repo: JobRepository,
    ) -> None:
        """When an archive hits the max_total_jobs (15) retry cap, the
        next fail(retry=True) must drive the archive to 'failed'.

        Before the fix, fail() at the cap just logged a warning and
        enqueued nothing — leaving the archive dangling in 'capturing'
        forever with no job to resolve it (observed live: 48 archives
        stuck this way). The archive must instead reach a terminal
        state so the UI shows the failure and offers Retry."""
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com/job-cap",
            )
            await archive_repo.update_status(
                conn, archive.id, ArchiveStatus.CAPTURING,
            )
            # Pile up jobs to the cap. enqueue() doesn't itself cap, so
            # this simulates an archive that churned through retries +
            # tier escalations until it accumulated the 15-job budget.
            last_job = None
            for _ in range(15):
                last_job = await job_repo.enqueue(
                    conn, archive.id, CaptureTier.CHROMIUM,
                )
            assert last_job is not None
            await job_repo.claim_next(conn, "worker-1")

            # Failing one more with retry=True is past the cap → no new
            # job, and the archive must be marked failed.
            await job_repo.fail(
                conn, last_job.id, "exhausted", retry=True,
            )

            reloaded = await archive_repo.get_by_id(conn, archive.id)
            assert reloaded is not None
            # The archive reached a terminal state instead of
            # dangling in 'capturing' forever.
            assert reloaded.status == ArchiveStatus.FAILED
            assert reloaded.error_message is not None
            assert "abandoned" in reloaded.error_message

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


class TestRepositoryEdgeCases:
    async def test_update_status_unknown_field_raises(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            archive = await archive_repo.create(
                conn, "https://example.com"
            )
            with pytest.raises(ValueError, match="Unknown"):
                await archive_repo.update_status(
                    conn,
                    archive.id,
                    ArchiveStatus.COMPLETE,
                    bogus_field="x",
                )

    async def test_archive_url_is_immutable_after_creation(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        """Cornerstone of the provenance contract: archive.url is the
        ORIGINAL user-submitted URL and must never be mutated, even
        when a fallback tier captures from a rewritten URL. The
        rewritten/fetched URL goes in metadata.source_url instead.

        Concretely: a privacy_frontend tier capture of twitter.com/foo
        stores ``archive.url = 'https://twitter.com/foo'`` (original)
        and ``metadata.source_url = 'https://xcancel.com/foo'`` (what
        actually got fetched). If a code path ever flipped these,
        users would lose the link back to the canonical URL — every
        detail page, search result, sitemap, social preview, and the
        'Captured from' provenance block would point at the fallback
        mirror, not the user's original submission.

        Pinned by asserting update_status rejects ``url=`` as an
        unknown field, which is the only path that COULD mutate the
        row from inside the application."""
        async with pool.acquire() as conn:
            original = "https://twitter.com/jack/status/20"
            archive = await archive_repo.create(conn, original)
            # update_status must reject url= as an unknown field —
            # that's the structural guarantee. metadata is the right
            # place for rewritten/fallback URLs.
            with pytest.raises(ValueError, match="Unknown update field: url"):
                await archive_repo.update_status(
                    conn,
                    archive.id,
                    ArchiveStatus.COMPLETE,
                    url="https://xcancel.com/jack/status/20",
                )
            # Sanity check: the row's url field stayed the original.
            reloaded = await archive_repo.get_by_id(conn, archive.id)
            assert reloaded is not None
            assert reloaded.url == original

    async def test_get_latest_complete(
        self,
        pool: asyncpg.pool.Pool,
        archive_repo: ArchiveRepository,
    ) -> None:
        async with pool.acquire() as conn:
            a1 = await archive_repo.create(
                conn, "https://example.com"
            )
            await archive_repo.update_status(
                conn, a1.id, ArchiveStatus.COMPLETE
            )

            result = await archive_repo.get_latest_complete(
                conn, a1.url_hash
            )
            assert result is not None
            assert result.id == a1.id


class TestProxyStatusRepository:
    async def test_record_and_list_passing(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = ProxyStatusRepository()
        async with pool.acquire() as conn:
            await repo.record(
                conn, "socks5://1.1.1.1:1080",
                gate_passing=True, asn_org="MTS", country_code="RU",
            )
            await repo.record(
                conn, "socks5://2.2.2.2:1080",
                gate_passing=False, asn_org="Hetzner", country_code="DE",
            )
            passing = await repo.list_passing(conn)
            assert passing == ["socks5://1.1.1.1:1080"]

    async def test_record_updates_on_conflict(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = ProxyStatusRepository()
        async with pool.acquire() as conn:
            await repo.record(conn, "socks5://a:1", gate_passing=False)
            await repo.record(conn, "socks5://a:1", gate_passing=False)
            await repo.record(conn, "socks5://a:1", gate_passing=False)
            # All failed; now succeed — failure counter must reset.
            await repo.record(conn, "socks5://a:1", gate_passing=True)
            fails_after_success = await conn.fetchval(
                "SELECT consecutive_failures FROM proxy_status "
                "WHERE proxy_server='socks5://a:1'"
            )
            assert fails_after_success == 0

    async def test_evict_dead(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = ProxyStatusRepository()
        async with pool.acquire() as conn:
            for _ in range(4):
                await repo.record(conn, "socks5://dead:1", gate_passing=False)
            await repo.record(conn, "socks5://alive:1", gate_passing=True)
            evicted = await repo.evict_dead(conn, failure_threshold=3)
            assert evicted == 1
            remaining = await conn.fetchval(
                "SELECT count(*) FROM proxy_status"
            )
            assert remaining == 1


class TestCfClearanceRepository:
    async def test_put_and_get(self, pool: asyncpg.pool.Pool) -> None:
        repo = CfClearanceRepository()
        async with pool.acquire() as conn:
            await repo.put(
                conn, "archive.ph", "cf_clearance", "abc123",
                proxy_server="socks5://p:1",
            )
            got = await repo.get(conn, "archive.ph", "socks5://p:1")
            assert got is not None
            assert got["value"] == "abc123"

    async def test_get_wrong_proxy_returns_none(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        """Cookie cached for one proxy is not returned for another."""
        repo = CfClearanceRepository()
        async with pool.acquire() as conn:
            await repo.put(
                conn, "archive.ph", "cf_clearance", "token",
                proxy_server="socks5://a:1",
            )
            assert await repo.get(conn, "archive.ph", "socks5://b:1") is None

    async def test_direct_connection_stored_separately(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        """Empty proxy_server (direct) is its own key space."""
        repo = CfClearanceRepository()
        async with pool.acquire() as conn:
            await repo.put(conn, "archive.ph", "cf_clearance", "direct_tok")
            await repo.put(
                conn, "archive.ph", "cf_clearance", "proxied_tok",
                proxy_server="socks5://p:1",
            )
            d = await repo.get(conn, "archive.ph", "")
            p = await repo.get(conn, "archive.ph", "socks5://p:1")
            assert d is not None and d["value"] == "direct_tok"
            assert p is not None and p["value"] == "proxied_tok"

    async def test_update_on_conflict(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = CfClearanceRepository()
        async with pool.acquire() as conn:
            await repo.put(conn, "archive.ph", "cf_clearance", "v1")
            await repo.put(conn, "archive.ph", "cf_clearance", "v2")
            got = await repo.get(conn, "archive.ph", "")
            assert got is not None and got["value"] == "v2"

    async def test_expired_not_returned(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = CfClearanceRepository()
        async with pool.acquire() as conn:
            # Manually insert an already-expired row
            await conn.execute(
                """
                INSERT INTO cf_clearance_cache
                    (domain, proxy_server, cookie_name, cookie_value,
                     cookie_path, expires_at)
                VALUES ('old.example', '', 'cf_clearance', 'dead', '/',
                        now() - interval '1 hour')
                """,
            )
            assert await repo.get(conn, "old.example", "") is None

    async def test_purge_expired(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = CfClearanceRepository()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cf_clearance_cache
                    (domain, proxy_server, cookie_name, cookie_value,
                     cookie_path, expires_at)
                VALUES ('a', '', 'cf_clearance', 'v', '/',
                        now() - interval '1 hour')
                """,
            )
            await repo.put(conn, "b", "cf_clearance", "live")
            purged = await repo.purge_expired(conn)
            assert purged == 1
            assert await repo.get(conn, "b", "") is not None


class TestDomainObservationsRepository:
    async def test_first_win_creates_row(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = DomainObservationsRepository()
        async with pool.acquire() as conn:
            await repo.record_outcome(
                conn, "example.com", CaptureTier.CHROMIUM, won=True,
            )
            row = await repo.get(conn, "example.com")
            assert row is not None
            assert row["tier_wins"] == {"chromium": 1}
            assert row["tier_losses"] == {}
            assert row["last_winning_tier"] == "chromium"

    async def test_first_loss_creates_row_with_null_winner(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = DomainObservationsRepository()
        async with pool.acquire() as conn:
            await repo.record_outcome(
                conn, "wsj.com", CaptureTier.CHROMIUM, won=False,
            )
            row = await repo.get(conn, "wsj.com")
            assert row is not None
            assert row["tier_losses"] == {"chromium": 1}
            assert row["tier_wins"] == {}
            assert row["last_winning_tier"] is None

    async def test_wins_accumulate(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = DomainObservationsRepository()
        async with pool.acquire() as conn:
            for _ in range(3):
                await repo.record_outcome(
                    conn, "example.com", CaptureTier.WAYBACK, won=True,
                )
            row = await repo.get(conn, "example.com")
            assert row is not None
            assert row["tier_wins"] == {"wayback": 3}

    async def test_mixed_tiers_separately_counted(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = DomainObservationsRepository()
        async with pool.acquire() as conn:
            await repo.record_outcome(
                conn, "nyt.com", CaptureTier.CHROMIUM, won=False,
            )
            await repo.record_outcome(
                conn, "nyt.com", CaptureTier.CAMOUFOX, won=False,
            )
            await repo.record_outcome(
                conn, "nyt.com", CaptureTier.WAYBACK, won=True,
            )
            row = await repo.get(conn, "nyt.com")
            assert row is not None
            assert row["tier_losses"] == {"chromium": 1, "camoufox": 1}
            assert row["tier_wins"] == {"wayback": 1}
            assert row["last_winning_tier"] == "wayback"

    async def test_last_winning_tier_updates(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        """Win with tier X sets last_winning_tier=X even after prior Y win."""
        repo = DomainObservationsRepository()
        async with pool.acquire() as conn:
            await repo.record_outcome(
                conn, "ex.com", CaptureTier.CHROMIUM, won=True,
            )
            await repo.record_outcome(
                conn, "ex.com", CaptureTier.CAMOUFOX, won=True,
            )
            row = await repo.get(conn, "ex.com")
            assert row is not None
            assert row["last_winning_tier"] == "camoufox"

    async def test_loss_does_not_overwrite_last_winning_tier(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        """After a win, a loss should not wipe last_winning_tier."""
        repo = DomainObservationsRepository()
        async with pool.acquire() as conn:
            await repo.record_outcome(
                conn, "ex.com", CaptureTier.CHROMIUM, won=True,
            )
            await repo.record_outcome(
                conn, "ex.com", CaptureTier.CAMOUFOX, won=False,
            )
            row = await repo.get(conn, "ex.com")
            assert row is not None
            assert row["last_winning_tier"] == "chromium"

    async def test_empty_apex_is_noop(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        """Malformed URLs produce empty apex — we silently skip rather
        than creating an empty-string catch-all row."""
        repo = DomainObservationsRepository()
        async with pool.acquire() as conn:
            await repo.record_outcome(
                conn, "", CaptureTier.CHROMIUM, won=True,
            )
            count = await conn.fetchval(
                "SELECT count(*) FROM domain_observations"
            )
            assert count == 0

    async def test_get_missing_returns_none(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = DomainObservationsRepository()
        async with pool.acquire() as conn:
            assert await repo.get(conn, "never-seen.com") is None


class TestFrontendStatusRepository:
    async def test_record_passing_then_list(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = FrontendStatusRepository()
        async with pool.acquire() as conn:
            await repo.record(
                conn, "https://scribe.rip", "medium.com",
                content_verified=True,
            )
            passing = await repo.list_passing(conn, "medium.com")
            assert passing == ["https://scribe.rip"]

    async def test_list_passing_excludes_failures(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = FrontendStatusRepository()
        async with pool.acquire() as conn:
            await repo.record(
                conn, "https://a", "reddit.com", content_verified=True,
            )
            await repo.record(
                conn, "https://b", "reddit.com", content_verified=False,
            )
            passing = await repo.list_passing(conn, "reddit.com")
            assert passing == ["https://a"]

    async def test_list_passing_scoped_by_apex(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = FrontendStatusRepository()
        async with pool.acquire() as conn:
            await repo.record(
                conn, "https://scribe.rip", "medium.com",
                content_verified=True,
            )
            await repo.record(
                conn, "https://xcancel.com", "twitter.com",
                content_verified=True,
            )
            m = await repo.list_passing(conn, "medium.com")
            t = await repo.list_passing(conn, "twitter.com")
            assert m == ["https://scribe.rip"]
            assert t == ["https://xcancel.com"]

    async def test_flip_from_passing_to_failing(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        """A pass followed by a fail flips verified off; failure counter bumps."""
        repo = FrontendStatusRepository()
        async with pool.acquire() as conn:
            await repo.record(
                conn, "https://inst", "reddit.com",
                content_verified=True,
            )
            await repo.record(
                conn, "https://inst", "reddit.com",
                content_verified=False,
            )
            row = await repo.get(conn, "https://inst", "reddit.com")
            assert row is not None
            assert row["content_verified"] is False
            assert row["consecutive_failures"] == 1
            assert await repo.list_passing(conn, "reddit.com") == []

    async def test_failure_counter_resets_on_pass(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = FrontendStatusRepository()
        async with pool.acquire() as conn:
            for _ in range(3):
                await repo.record(
                    conn, "https://inst", "medium.com",
                    content_verified=False,
                )
            row = await repo.get(conn, "https://inst", "medium.com")
            assert row is not None
            assert row["consecutive_failures"] == 3  # noqa: PLR2004
            await repo.record(
                conn, "https://inst", "medium.com",
                content_verified=True,
            )
            row2 = await repo.get(conn, "https://inst", "medium.com")
            assert row2 is not None
            assert row2["consecutive_failures"] == 0

    async def test_same_instance_two_apexes_independent(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        """xcancel.com fronts twitter.com AND x.com — separate rows."""
        repo = FrontendStatusRepository()
        async with pool.acquire() as conn:
            await repo.record(
                conn, "https://xcancel.com", "twitter.com",
                content_verified=True,
            )
            await repo.record(
                conn, "https://xcancel.com", "x.com",
                content_verified=False,
            )
            t = await repo.get(conn, "https://xcancel.com", "twitter.com")
            x = await repo.get(conn, "https://xcancel.com", "x.com")
            assert t is not None and t["content_verified"] is True
            assert x is not None and x["content_verified"] is False

    async def test_get_missing_returns_none(
        self, pool: asyncpg.pool.Pool
    ) -> None:
        repo = FrontendStatusRepository()
        async with pool.acquire() as conn:
            assert (
                await repo.get(conn, "https://unseen", "medium.com") is None
            )

