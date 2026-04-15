# ABOUTME: Job processing worker with LISTEN/NOTIFY and tier escalation
# ABOUTME: Polls PostgreSQL for capture jobs, runs pipeline, handles retries and fallbacks
"""Standalone worker process for capture job processing."""

from __future__ import annotations

import asyncio

import asyncpg
import structlog
from beartype import beartype

from archiver.browser_pool import BrowserPool
from archiver.capture import capture_page, save_artifacts
from archiver.config import Settings
from archiver.db import create_pool, init_db
from archiver.enums import (
    CLEARNET_TIER_ORDER,
    ArchiveStatus,
    CaptureTier,
)
from archiver.errors import AntiBotDetectedError, CaptureError
from archiver.models import JobRecord
from archiver.repository import ArchiveRepository, JobRepository, PgConnection

log = structlog.get_logger()


@beartype
def next_tier(current: CaptureTier) -> CaptureTier | None:
    """Return the next escalation tier, or None if exhausted."""
    try:
        idx = CLEARNET_TIER_ORDER.index(current)
    except ValueError:
        return None
    next_idx = idx + 1
    if next_idx >= len(CLEARNET_TIER_ORDER):
        return None
    return CLEARNET_TIER_ORDER[next_idx]


class Worker:
    """Capture job processor.

    Listens for PostgreSQL NOTIFY events and processes capture jobs
    with automatic tier escalation on anti-bot detection.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._browser_pool = BrowserPool(settings)
        self._archive_repo = ArchiveRepository()
        self._job_repo = JobRepository()
        self._semaphore = asyncio.Semaphore(
            settings.max_concurrent_captures
        )
        self._running = True
        self._pool: asyncpg.Pool | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @beartype
    async def run(self) -> None:  # pragma: no cover
        """Main worker loop."""
        self._pool = await create_pool(
            self._settings.db_url.get_secret_value(), min_size=2, max_size=5
        )
        await init_db(self._pool)

        # Reclaim stale jobs from dead workers
        async with self._pool.acquire() as conn:
            reclaimed = await self._job_repo.reclaim_stale(conn)
            if reclaimed > 0:
                log.info(
                    "worker.reclaimed_stale",
                    count=reclaimed,
                )

        # Set up LISTEN for new_job notifications
        listen_conn = await self._pool.acquire()
        try:
            await listen_conn.add_listener(
                "new_job", self._on_notify
            )

            log.info(
                "worker.started",
                worker_id=self._settings.worker_id,
            )

            while self._running:
                await self._claim_and_process()
                await asyncio.sleep(
                    self._settings.worker_poll_interval
                )
        finally:
            await listen_conn.remove_listener(
                "new_job", self._on_notify
            )
            await self._pool.release(listen_conn)
            await self._browser_pool.close()
            await self._pool.close()
            log.info("worker.stopped")

    # No @beartype — asyncpg callback, types guaranteed by the library
    def _on_notify(
        self,
        connection: PgConnection,
        pid: int,
        channel: str,
        payload: object,
    ) -> None:
        """Handle LISTEN/NOTIFY callback — wake the worker."""
        log.debug(
            "worker.notify_received",
            channel=channel,
            payload=payload,
        )

    @beartype
    async def _claim_and_process(self) -> None:  # pragma: no cover
        """Try to claim and process one job."""
        assert self._pool is not None  # noqa: S101
        async with self._pool.acquire() as conn:
            job = await self._job_repo.claim_next(
                conn, self._settings.worker_id
            )
        if job is None:
            return

        task = asyncio.create_task(self._process_job(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @beartype
    async def _process_job(self, job: JobRecord) -> None:
        """Process a single capture job with tier escalation."""
        async with self._semaphore:
            assert self._pool is not None  # noqa: S101
            async with self._pool.acquire() as conn:
                try:
                    await self._archive_repo.update_status(
                        conn,
                        job.archive_id,
                        ArchiveStatus.CAPTURING,
                    )

                    archive = await self._archive_repo.get_by_id(
                        conn, job.archive_id
                    )
                    if archive is None:
                        await self._job_repo.fail(
                            conn,
                            job.id,
                            "Archive deleted before capture",
                        )
                        return

                    browser = await self._browser_pool.get_browser(
                        job.tier
                    )
                    result = await capture_page(
                        url=archive.url,
                        browser=browser,
                        settings=self._settings,
                    )

                    artifact_dir = await save_artifacts(
                        result,
                        job.archive_id,
                        self._settings.artifacts_dir,
                    )

                    await self._archive_repo.update_status(
                        conn,
                        job.archive_id,
                        ArchiveStatus.COMPLETE,
                        title=result.title,
                        text_content=result.text_content,
                        artifact_dir=artifact_dir,
                        content_hash=result.content_hash,
                        screenshot_hash=result.screenshot_hash,
                        snapshot_size=len(result.snapshot_html),
                        warc_size=result.warc_size,
                    )
                    await self._job_repo.complete(conn, job.id)

                except AntiBotDetectedError as exc:
                    await self._handle_antibot(
                        conn, job, str(exc)
                    )

                except CaptureError as exc:
                    await self._handle_capture_error(
                        conn, job, str(exc)
                    )

                except Exception as exc:
                    log.exception(
                        "worker.unexpected_error",
                        job_id=job.id,
                    )
                    await self._handle_capture_error(
                        conn, job, str(exc)
                    )

    # @beartype — private, conn is AsyncMock in tests
    async def _handle_antibot(
        self,
        conn: PgConnection,
        job: JobRecord,
        error: str,
    ) -> None:
        """Escalate to next tier on anti-bot detection."""
        escalated = next_tier(job.tier)
        await self._job_repo.fail(
            conn, job.id, error, retry=False
        )

        if escalated is not None:
            log.warning(
                "worker.tier_escalation",
                job_id=job.id,
                from_tier=job.tier.value,
                to_tier=escalated.value,
            )
            await self._job_repo.enqueue(
                conn,
                job.archive_id,
                escalated,
                priority=job.priority + 1,
            )
        else:
            log.error(
                "worker.all_tiers_exhausted",
                job_id=job.id,
            )
            await self._archive_repo.update_status(
                conn,
                job.archive_id,
                ArchiveStatus.FAILED,
                error_message="All capture tiers exhausted: "
                + error,
            )

    # @beartype — private, conn is AsyncMock in tests
    async def _handle_capture_error(
        self,
        conn: PgConnection,
        job: JobRecord,
        error: str,
    ) -> None:
        """Handle capture failure with optional retry."""
        if job.attempts < job.max_attempts:
            log.warning(
                "worker.retry",
                job_id=job.id,
                attempt=job.attempts,
                max=job.max_attempts,
            )
            await self._job_repo.fail(
                conn, job.id, error, retry=True
            )
        else:
            log.error(
                "worker.max_retries_exceeded",
                job_id=job.id,
            )
            await self._job_repo.fail(
                conn, job.id, error, retry=False
            )
            await self._archive_repo.update_status(
                conn,
                job.archive_id,
                ArchiveStatus.FAILED,
                error_message=error,
            )

    @beartype
    async def shutdown(self) -> None:
        """Gracefully stop the worker."""
        log.info("worker.shutting_down")
        self._running = False
