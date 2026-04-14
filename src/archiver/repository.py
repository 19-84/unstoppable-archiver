# ABOUTME: Data access layer with ArchiveRepository and JobRepository
# ABOUTME: Handles CRUD, FTS search, job claiming (SKIP LOCKED), NOTIFY, and dedup checks
"""Data access layer for archives and jobs."""

from __future__ import annotations

from typing import Any

import asyncpg
import asyncpg.pool
import structlog
from beartype import beartype
from ulid import ULID

from archiver.enums import ArchiveStatus, CaptureSource, CaptureTier, JobStatus
from archiver.models import ArchiveRecord, JobRecord, SearchResult
from archiver.url import normalize_url, url_hash

# asyncpg.pool.PoolConnectionProxy is returned by pool.acquire()
# but is not a subclass of asyncpg.Connection in the stubs.
PgConnection = asyncpg.Connection | asyncpg.pool.PoolConnectionProxy

log = structlog.get_logger()


def _ulid() -> str:
    return str(ULID())


@beartype
def _record_to_archive(row: asyncpg.Record) -> ArchiveRecord:
    return ArchiveRecord(
        id=row["id"],
        url=row["url"],
        url_hash=row["url_hash"],
        title=row["title"],
        text_content=row["text_content"],
        status=ArchiveStatus(row["status"]),
        tier=CaptureTier(row["tier"]),
        source=CaptureSource(row["source"]),
        error_message=row["error_message"],
        artifact_dir=row["artifact_dir"],
        content_hash=row["content_hash"],
        screenshot_hash=row["screenshot_hash"],
        revisit_of=row["revisit_of"],
        snapshot_size=row["snapshot_size"],
        warc_size=row["warc_size"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
    )


@beartype
def _record_to_job(row: asyncpg.Record) -> JobRecord:
    return JobRecord(
        id=row["id"],
        archive_id=row["archive_id"],
        status=JobStatus(row["status"]),
        tier=CaptureTier(row["tier"]),
        priority=row["priority"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error_message=row["error_message"],
        locked_by=row["locked_by"],
        locked_at=row["locked_at"],
    )


_ARCHIVE_COLS = (
    "id, url, url_hash, title, text_content, status, tier, source, "
    "error_message, artifact_dir, content_hash, screenshot_hash, "
    "revisit_of, snapshot_size, warc_size, created_at, completed_at"
)

_JOB_COLS = (
    "id, archive_id, status, tier, priority, attempts, max_attempts, "
    "created_at, started_at, completed_at, error_message, locked_by, "
    "locked_at"
)

# Pre-built SQL — column lists are module-level string constants, not user input.
_SQL_INSERT_ARCHIVE = (
    "INSERT INTO archives (id, url, url_hash, status, tier)"
    " VALUES ($1, $2, $3, $4, $5)"
    " RETURNING " + _ARCHIVE_COLS
)
_SQL_SELECT_ARCHIVE = "SELECT " + _ARCHIVE_COLS + " FROM archives WHERE id = $1"
_SQL_SELECT_BY_URL_HASH = (
    "SELECT " + _ARCHIVE_COLS
    + " FROM archives WHERE url_hash = $1 ORDER BY created_at DESC"
)
_SQL_SELECT_LATEST_COMPLETE = (
    "SELECT " + _ARCHIVE_COLS + " FROM archives"
    " WHERE url_hash = $1 AND status = $2"
    " ORDER BY completed_at DESC LIMIT 1"
)
_SQL_CHECK_RECENT = (
    "SELECT " + _ARCHIVE_COLS + " FROM archives"
    " WHERE url_hash = $1 AND status = $2"
    " AND completed_at > now() - make_interval(secs => $3)"
    " ORDER BY completed_at DESC LIMIT 1"
)
_SQL_REVISIT = (
    "UPDATE archives"
    " SET revisit_of = $2, content_hash = $3, status = $4, completed_at = now()"
    " WHERE id = $1 RETURNING " + _ARCHIVE_COLS
)
_SQL_SEARCH = (
    "SELECT " + _ARCHIVE_COLS + ","
    " ts_rank(search_vector, websearch_to_tsquery('english', $1)) AS rank"
    " FROM archives"
    " WHERE search_vector @@ websearch_to_tsquery('english', $1) AND status = $2"
    " ORDER BY rank DESC, created_at DESC"
    " LIMIT $3 OFFSET $4"
)
_SQL_LIST_RECENT = (
    "SELECT " + _ARCHIVE_COLS + " FROM archives"
    " ORDER BY created_at DESC LIMIT $1 OFFSET $2"
)
_SQL_INSERT_JOB = (
    "INSERT INTO jobs (id, archive_id, status, tier, priority)"
    " VALUES ($1, $2, $3, $4, $5)"
    " RETURNING " + _JOB_COLS
)
_SQL_CLAIM_NEXT = (
    "UPDATE jobs"
    " SET status = $1, locked_by = $2, started_at = now(),"
    " locked_at = now(), attempts = attempts + 1"
    " WHERE id = ("
    "   SELECT id FROM jobs WHERE status = $3"
    "   ORDER BY priority DESC, created_at"
    "   FOR UPDATE SKIP LOCKED LIMIT 1"
    " ) RETURNING " + _JOB_COLS
)
_SQL_COMPLETE_JOB = (
    "UPDATE jobs SET status = $1, completed_at = now()"
    " WHERE id = $2 RETURNING " + _JOB_COLS
)
_SQL_FAIL_JOB = (
    "UPDATE jobs SET status = $1, error_message = $2, completed_at = now()"
    " WHERE id = $3 RETURNING " + _JOB_COLS
)
_SQL_SELECT_JOB = "SELECT " + _JOB_COLS + " FROM jobs WHERE id = $1"


class ArchiveRepository:
    """CRUD operations for archives."""

    @beartype
    async def create(
        self,
        conn: PgConnection,
        url: str,
        *,
        priority: int = 0,
    ) -> ArchiveRecord:
        """Create a new archive record and return it."""
        archive_id = _ulid()
        normalized = normalize_url(url)
        uhash = url_hash(url)

        row = await conn.fetchrow(
            _SQL_INSERT_ARCHIVE,
            archive_id,
            normalized,
            uhash,
            ArchiveStatus.PENDING.value,
            CaptureTier.CHROMIUM.value,
        )
        assert row is not None  # noqa: S101
        log.info("archive.created", archive_id=archive_id, url=normalized)
        return _record_to_archive(row)

    @beartype
    async def get_by_id(
        self, conn: PgConnection, archive_id: str
    ) -> ArchiveRecord | None:
        """Fetch a single archive by ID."""
        row = await conn.fetchrow(_SQL_SELECT_ARCHIVE, archive_id)
        return _record_to_archive(row) if row else None

    @beartype
    async def get_by_url_hash(
        self, conn: PgConnection, uhash: str
    ) -> list[ArchiveRecord]:
        """Fetch all archives for a URL hash, most recent first."""
        rows = await conn.fetch(_SQL_SELECT_BY_URL_HASH, uhash)
        return [_record_to_archive(r) for r in rows]

    @beartype
    async def get_latest_complete(
        self, conn: PgConnection, uhash: str
    ) -> ArchiveRecord | None:
        """Get the most recent complete archive for a URL hash."""
        row = await conn.fetchrow(
            _SQL_SELECT_LATEST_COMPLETE,
            uhash,
            ArchiveStatus.COMPLETE.value,
        )
        return _record_to_archive(row) if row else None

    @beartype
    async def check_recent_capture(
        self,
        conn: PgConnection,
        uhash: str,
        interval_seconds: int,
    ) -> ArchiveRecord | None:
        """Check if a URL was captured within the recapture interval."""
        row = await conn.fetchrow(
            _SQL_CHECK_RECENT,
            uhash,
            ArchiveStatus.COMPLETE.value,
            float(interval_seconds),
        )
        return _record_to_archive(row) if row else None

    @beartype
    async def update_status(
        self,
        conn: PgConnection,
        archive_id: str,
        status: ArchiveStatus,
        **kwargs: Any,
    ) -> ArchiveRecord | None:
        """Update archive status and optional fields."""
        set_parts = ["status = $2"]
        params: list[Any] = [archive_id, status.value]
        idx = 3

        # Auto-set completed_at when transitioning to terminal states
        if (
            status in (ArchiveStatus.COMPLETE, ArchiveStatus.FAILED)
            and "completed_at" not in kwargs
        ):
                set_parts.append("completed_at = now()")

        allowed_fields = {
            "title", "text_content", "error_message", "artifact_dir",
            "content_hash", "screenshot_hash", "revisit_of",
            "snapshot_size", "warc_size", "completed_at", "tier",
            "source",
        }

        for key, value in kwargs.items():
            if key not in allowed_fields:
                msg = f"Unknown update field: {key}"
                raise ValueError(msg)
            set_parts.append(key + " = $" + str(idx))
            params.append(value)
            idx += 1

        set_clause = ", ".join(set_parts)
        sql = (
            "UPDATE archives SET " + set_clause
            + " WHERE id = $1 RETURNING " + _ARCHIVE_COLS
        )
        row = await conn.fetchrow(sql, *params)
        return _record_to_archive(row) if row else None

    @beartype
    async def create_revisit(
        self,
        conn: PgConnection,
        archive_id: str,
        revisit_of_id: str,
        content_hash: str,
    ) -> ArchiveRecord | None:
        """Mark an archive as a revisit of an existing one."""
        row = await conn.fetchrow(
            _SQL_REVISIT,
            archive_id,
            revisit_of_id,
            content_hash,
            ArchiveStatus.COMPLETE.value,
        )
        return _record_to_archive(row) if row else None

    @beartype
    async def search(
        self,
        conn: PgConnection,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> SearchResult:
        """Full-text search across archived pages."""
        count_row = await conn.fetchrow(
            "SELECT count(*) FROM archives"
            " WHERE search_vector @@ websearch_to_tsquery('english', $1)"
            " AND status = $2",
            query,
            ArchiveStatus.COMPLETE.value,
        )
        total = count_row["count"] if count_row else 0

        rows = await conn.fetch(
            _SQL_SEARCH,
            query,
            ArchiveStatus.COMPLETE.value,
            limit,
            offset,
        )
        return SearchResult(
            archives=[_record_to_archive(r) for r in rows],
            total=total,
            query=query,
        )

    @beartype
    async def list_recent(
        self,
        conn: PgConnection,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ArchiveRecord], int]:
        """List archives, most recent first."""
        count_row = await conn.fetchrow(
            "SELECT count(*) FROM archives"
        )
        total = count_row["count"] if count_row else 0

        rows = await conn.fetch(_SQL_LIST_RECENT, limit, offset)
        return [_record_to_archive(r) for r in rows], total

    @beartype
    async def delete(
        self, conn: PgConnection, archive_id: str
    ) -> bool:
        """Delete an archive and its jobs (CASCADE)."""
        result = await conn.execute(
            "DELETE FROM archives WHERE id = $1", archive_id
        )
        return result == "DELETE 1"


class JobRepository:
    """CRUD operations for the job queue."""

    @beartype
    async def enqueue(
        self,
        conn: PgConnection,
        archive_id: str,
        tier: CaptureTier,
        *,
        priority: int = 0,
    ) -> JobRecord:
        """Create a new job and notify workers."""
        job_id = _ulid()
        row = await conn.fetchrow(
            _SQL_INSERT_JOB,
            job_id,
            archive_id,
            JobStatus.QUEUED.value,
            tier.value,
            priority,
        )
        assert row is not None  # noqa: S101

        await conn.execute("SELECT pg_notify('new_job', $1)", job_id)

        log.info(
            "job.enqueued",
            job_id=job_id,
            archive_id=archive_id,
            tier=tier.value,
        )
        return _record_to_job(row)

    @beartype
    async def claim_next(
        self, conn: PgConnection, worker_id: str
    ) -> JobRecord | None:
        """Atomically claim the next queued job."""
        row = await conn.fetchrow(
            _SQL_CLAIM_NEXT,
            JobStatus.RUNNING.value,
            worker_id,
            JobStatus.QUEUED.value,
        )
        if row:
            log.info(
                "job.claimed", job_id=row["id"], worker_id=worker_id
            )
            return _record_to_job(row)
        return None

    @beartype
    async def complete(
        self, conn: PgConnection, job_id: str
    ) -> JobRecord | None:
        """Mark a job as complete."""
        row = await conn.fetchrow(
            _SQL_COMPLETE_JOB, JobStatus.COMPLETE.value, job_id
        )
        if row:
            log.info("job.completed", job_id=job_id)
            return _record_to_job(row)
        return None

    @beartype
    async def fail(
        self,
        conn: PgConnection,
        job_id: str,
        error: str,
        *,
        retry: bool = False,
    ) -> JobRecord | None:
        """Mark a job as failed, optionally re-queue for retry."""
        status = JobStatus.RETRY if retry else JobStatus.FAILED
        row = await conn.fetchrow(
            _SQL_FAIL_JOB, status.value, error, job_id
        )
        if row and retry:
            job = _record_to_job(row)
            await self.enqueue(
                conn, job.archive_id, job.tier, priority=job.priority
            )
        if row:
            log.info(
                "job.failed", job_id=job_id, retry=retry, error=error
            )
            return _record_to_job(row)
        return None

    @beartype
    async def reclaim_stale(
        self, conn: PgConnection, stale_seconds: int = 300
    ) -> int:
        """Reclaim jobs locked longer than stale_seconds."""
        result = await conn.execute(
            "UPDATE jobs"
            " SET status = 'queued', locked_by = NULL,"
            " locked_at = NULL, started_at = NULL"
            " WHERE status = 'running'"
            " AND locked_at < now() - make_interval(secs => $1)",
            float(stale_seconds),
        )
        count = int(result.split()[-1]) if result else 0
        if count > 0:
            log.warning("jobs.reclaimed_stale", count=count)
        return count

    @beartype
    async def get_by_id(
        self, conn: PgConnection, job_id: str
    ) -> JobRecord | None:
        """Fetch a job by ID."""
        row = await conn.fetchrow(_SQL_SELECT_JOB, job_id)
        return _record_to_job(row) if row else None
