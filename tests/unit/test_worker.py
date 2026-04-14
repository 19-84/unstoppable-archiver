# ABOUTME: Unit tests for worker tier escalation and job processing logic
# ABOUTME: Tests next_tier, process_job success/failure/antibot paths with mocked deps
"""Tests for worker logic."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg

from archiver.config import Settings
from archiver.enums import (
    ArchiveStatus,
    CaptureSource,
    CaptureTier,
    JobStatus,
)
from archiver.errors import AntiBotDetectedError, CaptureError
from archiver.models import ArchiveRecord, CaptureResult, JobRecord
from archiver.worker import Worker, next_tier


def _make_job(
    tier: CaptureTier = CaptureTier.CHROMIUM,
    attempts: int = 1,
    max_attempts: int = 3,
) -> JobRecord:
    return JobRecord(
        id="job-1",
        archive_id="arc-1",
        status=JobStatus.RUNNING,
        tier=tier,
        priority=0,
        attempts=attempts,
        max_attempts=max_attempts,
        created_at=datetime.now(UTC),
    )


def _make_archive() -> ArchiveRecord:
    return ArchiveRecord(
        id="arc-1",
        url="https://example.com",
        url_hash="abc123",
        status=ArchiveStatus.CAPTURING,
        tier=CaptureTier.CHROMIUM,
        source=CaptureSource.DIRECT,
        created_at=datetime.now(UTC),
    )


def _make_capture_result() -> CaptureResult:
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (10, 10), "red")
    buf = BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    return CaptureResult(
        snapshot_html=b"<html>test</html>",
        screenshot_png=png,
        thumbnail_png=png,
        text_content="test text",
        title="Test",
        warc_path=None,
        warc_size=0,
        content_hash="abc",
        screenshot_hash="def",
    )


def _make_pool_mock() -> tuple[MagicMock, AsyncMock]:
    """Create a mock pool where acquire() returns an async ctx mgr."""
    mock_conn = AsyncMock()
    mock_acquire_ctx = AsyncMock()
    mock_acquire_ctx.__aenter__ = AsyncMock(
        return_value=mock_conn
    )
    mock_acquire_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value = mock_acquire_ctx
    return mock_pool, mock_conn


def _make_worker() -> tuple[Worker, AsyncMock]:
    settings = Settings()
    worker = Worker(settings)
    mock_pool, mock_conn = _make_pool_mock()
    worker._pool = mock_pool
    worker._browser_pool = AsyncMock()
    worker._archive_repo = AsyncMock()
    worker._job_repo = AsyncMock()
    return worker, mock_conn


class TestNextTier:
    def test_chromium_escalates_to_camoufox(self) -> None:
        assert (
            next_tier(CaptureTier.CHROMIUM)
            == CaptureTier.CAMOUFOX
        )

    def test_camoufox_escalates_to_proxy(self) -> None:
        assert (
            next_tier(CaptureTier.CAMOUFOX)
            == CaptureTier.CAMOUFOX_PROXY
        )

    def test_proxy_escalates_to_wayback(self) -> None:
        assert (
            next_tier(CaptureTier.CAMOUFOX_PROXY)
            == CaptureTier.WAYBACK
        )

    def test_wayback_escalates_to_archive_today(self) -> None:
        assert (
            next_tier(CaptureTier.WAYBACK)
            == CaptureTier.ARCHIVE_TODAY
        )

    def test_archive_today_returns_none(self) -> None:
        assert next_tier(CaptureTier.ARCHIVE_TODAY) is None

    def test_full_chain_has_five_tiers(self) -> None:
        tiers: list[CaptureTier] = [CaptureTier.CHROMIUM]
        tier: CaptureTier | None = CaptureTier.CHROMIUM
        while (tier := next_tier(tier)) is not None:
            tiers.append(tier)
        total_tiers = 5
        assert len(tiers) == total_tiers


class TestWorkerProcessJob:
    @patch("archiver.worker.save_artifacts", new_callable=AsyncMock)
    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_success(
        self,
        mock_capture: AsyncMock,
        mock_save: AsyncMock,
    ) -> None:
        worker, _conn = _make_worker()
        job = _make_job()
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.return_value = _make_capture_result()
        mock_save.return_value = "abc123/20260414"
        worker._browser_pool.get_browser = AsyncMock(
            return_value=AsyncMock()
        )

        await worker._process_job(job)

        worker._job_repo.complete.assert_awaited_once()

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_antibot_escalates(
        self, mock_capture: AsyncMock
    ) -> None:
        worker, _conn = _make_worker()
        job = _make_job(tier=CaptureTier.CHROMIUM)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.side_effect = AntiBotDetectedError(
            "Blocked"
        )
        worker._browser_pool.get_browser = AsyncMock()

        await worker._process_job(job)

        worker._job_repo.fail.assert_awaited_once()
        worker._job_repo.enqueue.assert_awaited_once()
        enqueue_args = worker._job_repo.enqueue.call_args
        # enqueue(conn, archive_id, tier, priority=...)
        assert enqueue_args[0][2] == CaptureTier.CAMOUFOX

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_antibot_tiers_exhausted(
        self, mock_capture: AsyncMock
    ) -> None:
        worker, _conn = _make_worker()
        job = _make_job(tier=CaptureTier.ARCHIVE_TODAY)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.side_effect = AntiBotDetectedError(
            "Still blocked"
        )
        worker._browser_pool.get_browser = AsyncMock()

        await worker._process_job(job)

        # Should mark archive as FAILED
        calls = worker._archive_repo.update_status.call_args_list
        fail_call = [
            c
            for c in calls
            if len(c[0]) >= 3  # noqa: PLR2004
            and c[0][2] == ArchiveStatus.FAILED
        ]
        assert len(fail_call) == 1

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_capture_error_retries(
        self, mock_capture: AsyncMock
    ) -> None:
        worker, _conn = _make_worker()
        job = _make_job(attempts=1, max_attempts=3)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.side_effect = CaptureError("timeout")
        worker._browser_pool.get_browser = AsyncMock()

        await worker._process_job(job)

        fail_kwargs = worker._job_repo.fail.call_args
        assert fail_kwargs[1]["retry"] is True

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_capture_error_max_retries(
        self, mock_capture: AsyncMock
    ) -> None:
        worker, _conn = _make_worker()
        job = _make_job(attempts=3, max_attempts=3)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.side_effect = CaptureError("timeout")
        worker._browser_pool.get_browser = AsyncMock()

        await worker._process_job(job)

        fail_kwargs = worker._job_repo.fail.call_args
        assert fail_kwargs[1]["retry"] is False

    async def test_archive_deleted_before_capture(
        self,
    ) -> None:
        worker, _conn = _make_worker()
        job = _make_job()
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=None
        )

        await worker._process_job(job)

        worker._job_repo.fail.assert_awaited_once()
        fail_args = worker._job_repo.fail.call_args
        # fail(conn, job_id, error_message)
        assert "deleted" in fail_args[0][2].lower()


class TestWorkerShutdown:
    async def test_shutdown_sets_running_false(self) -> None:
        worker = Worker(Settings())
        assert worker._running is True
        await worker.shutdown()
        assert worker._running is False


class TestWorkerOnNotify:
    def test_on_notify_does_not_crash(self) -> None:
        worker = Worker(Settings())
        mock_conn = MagicMock(spec=asyncpg.Connection)
        worker._on_notify(mock_conn, 0, "new_job", "job-1")
