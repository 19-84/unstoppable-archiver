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
    worker._obs_repo = AsyncMock()
    proxy_status_repo = AsyncMock()
    # list_passing returns [] by default — tier-5 falls through to direct.
    proxy_status_repo.list_passing = AsyncMock(return_value=[])
    worker._proxy_status_repo = proxy_status_repo
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

    def test_proxy_escalates_to_privacy_frontend(self) -> None:
        assert (
            next_tier(CaptureTier.CAMOUFOX_PROXY)
            == CaptureTier.PRIVACY_FRONTEND
        )

    def test_privacy_frontend_escalates_to_wayback(self) -> None:
        assert (
            next_tier(CaptureTier.PRIVACY_FRONTEND)
            == CaptureTier.WAYBACK
        )

    def test_wayback_escalates_to_archive_today(self) -> None:
        assert (
            next_tier(CaptureTier.WAYBACK)
            == CaptureTier.ARCHIVE_TODAY
        )

    def test_archive_today_escalates_to_commoncrawl(self) -> None:
        assert (
            next_tier(CaptureTier.ARCHIVE_TODAY)
            == CaptureTier.COMMONCRAWL
        )

    def test_commoncrawl_escalates_to_archive_today_submit(self) -> None:
        assert (
            next_tier(CaptureTier.COMMONCRAWL)
            == CaptureTier.ARCHIVE_TODAY_SUBMIT
        )

    def test_archive_today_submit_returns_none(self) -> None:
        assert next_tier(CaptureTier.ARCHIVE_TODAY_SUBMIT) is None

    def test_full_chain_has_eight_tiers(self) -> None:
        tiers: list[CaptureTier] = [CaptureTier.CHROMIUM]
        tier: CaptureTier | None = CaptureTier.CHROMIUM
        while (tier := next_tier(tier)) is not None:
            tiers.append(tier)
        total_tiers = 8
        assert len(tiers) == total_tiers

    def test_unknown_tier_returns_none(self) -> None:
        """A tier not in CLEARNET_TIER_ORDER (e.g. darknet-only) yields None."""
        import archiver.worker as worker_mod

        orig = worker_mod.CLEARNET_TIER_ORDER
        try:
            worker_mod.CLEARNET_TIER_ORDER = [CaptureTier.CHROMIUM]
            assert next_tier(CaptureTier.CAMOUFOX) is None
        finally:
            worker_mod.CLEARNET_TIER_ORDER = orig


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
        # Use CAMOUFOX_PROXY (last direct tier) so antibot exhausts all tiers
        job = _make_job(tier=CaptureTier.CAMOUFOX_PROXY)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.side_effect = AntiBotDetectedError(
            "Still blocked"
        )
        worker._browser_pool.get_browser = AsyncMock()

        await worker._process_job(job)

        # Antibot escalates to PRIVACY_FRONTEND tier (not exhausted yet)
        worker._job_repo.enqueue.assert_awaited_once()
        enqueue_args = worker._job_repo.enqueue.call_args
        assert enqueue_args[0][2] == CaptureTier.PRIVACY_FRONTEND

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_capture_error_retries(
        self, mock_capture: AsyncMock
    ) -> None:
        worker, mock_conn = _make_worker()
        job = _make_job(attempts=1, max_attempts=3)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.side_effect = CaptureError("timeout")
        worker._browser_pool.get_browser = AsyncMock()
        # Simulate 1 existing job for this tier (below max, should retry)
        mock_conn.fetchval = AsyncMock(return_value=1)

        await worker._process_job(job)

        fail_kwargs = worker._job_repo.fail.call_args
        assert fail_kwargs[1]["retry"] is True

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_capture_error_max_retries(
        self, mock_capture: AsyncMock
    ) -> None:
        worker, mock_conn = _make_worker()
        job = _make_job(attempts=3, max_attempts=3)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.side_effect = CaptureError("timeout")
        worker._browser_pool.get_browser = AsyncMock()
        # Simulate 3 existing jobs for this tier (triggers escalation)
        mock_conn.fetchval = AsyncMock(return_value=3)

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


class TestWorkerFallbackTiers:
    @patch("archiver.worker.save_artifacts", new_callable=AsyncMock, return_value="hash/20260101")
    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    @patch("archiver.worker.check_wayback_availability", new_callable=AsyncMock)
    async def test_wayback_tier_uses_fallback(
        self,
        mock_wayback: AsyncMock,
        mock_capture: AsyncMock,
        mock_save: AsyncMock,
    ) -> None:
        worker, _ = _make_worker()
        job = _make_job(tier=CaptureTier.WAYBACK)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_wayback.return_value = "https://web.archive.org/web/2024/https://example.com"
        mock_capture.return_value = _make_capture_result()
        worker._browser_pool.get_browser = AsyncMock()

        await worker._process_job(job)

        mock_wayback.assert_awaited_once()
        mock_capture.assert_awaited_once()
        worker._job_repo.complete.assert_awaited_once()

    @patch("archiver.worker.check_wayback_availability", new_callable=AsyncMock)
    async def test_wayback_not_available_raises(
        self,
        mock_wayback: AsyncMock,
    ) -> None:
        worker, mock_conn = _make_worker()
        job = _make_job(tier=CaptureTier.WAYBACK)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_wayback.return_value = None
        mock_conn.fetchval = AsyncMock(return_value=1)

        await worker._process_job(job)

        worker._job_repo.fail.assert_awaited()

    @patch("archiver.worker.fetch_archive_today_snapshot_html", new_callable=AsyncMock)
    @patch("archiver.worker.find_archive_today_snapshot", new_callable=AsyncMock)
    async def test_archive_today_tier_direct_fetch_success(
        self,
        mock_find: AsyncMock,
        mock_fetch: AsyncMock,
    ) -> None:
        """Direct-fetch short-circuit: timemap found + html fetched via httpx."""
        worker, _ = _make_worker()
        job = _make_job(tier=CaptureTier.ARCHIVE_TODAY)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_find.return_value = "https://archive.today/2024/foo"
        mock_fetch.return_value = (
            "<html><title>Real Article</title>"
            "<body>actual content here</body></html>"
        )

        with patch(
            "archiver.worker.save_artifacts",
            new_callable=AsyncMock,
            return_value="hash/20260101",
        ):
            await worker._process_job(job)

        mock_find.assert_awaited_once()
        mock_fetch.assert_awaited_once()
        # Direct-fetch path — browser NOT invoked
        assert not worker._browser_pool.get_browser.called
        worker._job_repo.complete.assert_awaited_once()

    @patch("archiver.worker.save_artifacts", new_callable=AsyncMock, return_value="hash/20260101")
    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    @patch("archiver.worker.fetch_archive_today_snapshot_html", new_callable=AsyncMock)
    @patch("archiver.worker.find_archive_today_snapshot", new_callable=AsyncMock)
    async def test_archive_today_tier_falls_back_to_browser(
        self,
        mock_find: AsyncMock,
        mock_fetch: AsyncMock,
        mock_capture: AsyncMock,
        mock_save: AsyncMock,
    ) -> None:
        """Direct-fetch returns None (CF blocked) → Camoufox renders."""
        worker, _ = _make_worker()
        job = _make_job(tier=CaptureTier.ARCHIVE_TODAY)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_find.return_value = "https://archive.today/2024/foo"
        mock_fetch.return_value = None  # simulate CF block
        mock_capture.return_value = _make_capture_result()
        worker._browser_pool.get_browser = AsyncMock()

        await worker._process_job(job)

        mock_capture.assert_awaited_once()
        worker._job_repo.complete.assert_awaited_once()

    @patch("archiver.worker.cc_find_snapshot_full_history", new_callable=AsyncMock)
    @patch("archiver.worker.cc_find_snapshot", new_callable=AsyncMock)
    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    @patch("archiver.worker.find_archive_today_snapshot", new_callable=AsyncMock)
    async def test_capture_error_all_tiers_exhausted(
        self,
        mock_find: AsyncMock,
        mock_capture: AsyncMock,
        mock_cc: AsyncMock,
        mock_cc_full: AsyncMock,
    ) -> None:
        """Exhausting the last tier (ARCHIVE_TODAY_SUBMIT) marks FAILED."""
        worker, mock_conn = _make_worker()
        job = _make_job(
            tier=CaptureTier.ARCHIVE_TODAY_SUBMIT,
            attempts=3,
            max_attempts=3,
        )
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        # No gate-passers in the pool → submit tier raises immediately.
        worker._proxy_status_repo.list_passing = AsyncMock(return_value=[])
        mock_find.return_value = None
        mock_cc.return_value = None
        mock_cc_full.return_value = None
        worker._browser_pool.get_browser = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=3)

        await worker._process_job(job)

        calls = worker._archive_repo.update_status.call_args_list
        fail_call = [
            c for c in calls
            if len(c[0]) >= 3 and c[0][2] == ArchiveStatus.FAILED  # noqa: PLR2004
        ]
        assert len(fail_call) == 1

    @patch("archiver.worker.find_archive_today_snapshot", new_callable=AsyncMock)
    async def test_archive_today_no_snapshot_raises(
        self,
        mock_find: AsyncMock,
    ) -> None:
        worker, mock_conn = _make_worker()
        job = _make_job(tier=CaptureTier.ARCHIVE_TODAY)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_find.return_value = None
        mock_conn.fetchval = AsyncMock(return_value=1)

        await worker._process_job(job)
        worker._job_repo.fail.assert_awaited()

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_unexpected_error_handled(
        self,
        mock_capture: AsyncMock,
    ) -> None:
        worker, mock_conn = _make_worker()
        job = _make_job()
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.side_effect = RuntimeError("unexpected")
        worker._browser_pool.get_browser = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value=1)

        await worker._process_job(job)
        worker._job_repo.fail.assert_awaited()


class TestCaptureViaCommonCrawl:
    @patch("archiver.worker.cc_fetch_record_html", new_callable=AsyncMock)
    @patch("archiver.worker.cc_find_snapshot", new_callable=AsyncMock)
    async def test_happy_path_returns_result(
        self,
        mock_find: AsyncMock,
        mock_fetch: AsyncMock,
    ) -> None:
        from archiver.commoncrawl import CCSnapshot

        worker, _ = _make_worker()
        mock_find.return_value = CCSnapshot(
            url="https://example.com/",
            timestamp="20260101120000",
            crawl_id="CC-MAIN-2026-12",
            filename="x.warc.gz",
            offset=0, length=100, status=200, mime="text/html",
        )
        mock_fetch.return_value = b"<html><body>cc body</body></html>"
        result = await worker._capture_via_commoncrawl("https://example.com/")
        assert b"cc body" in result.snapshot_html

    @patch("archiver.worker.cc_find_snapshot_full_history", new_callable=AsyncMock)
    @patch("archiver.worker.cc_find_snapshot", new_callable=AsyncMock)
    async def test_no_snapshot_raises(
        self, mock_find: AsyncMock, mock_full: AsyncMock
    ) -> None:
        worker, _ = _make_worker()
        mock_find.return_value = None
        mock_full.return_value = None
        import pytest as _pt

        with _pt.raises(CaptureError, match="No Common Crawl snapshot"):
            await worker._capture_via_commoncrawl("https://example.com/")

    @patch("archiver.worker.cc_fetch_record_html", new_callable=AsyncMock)
    @patch("archiver.worker.cc_find_snapshot_full_history", new_callable=AsyncMock)
    @patch("archiver.worker.cc_find_snapshot", new_callable=AsyncMock)
    async def test_recent_miss_falls_to_deep_scan(
        self,
        mock_find: AsyncMock,
        mock_full: AsyncMock,
        mock_fetch: AsyncMock,
    ) -> None:
        """Recent-crawl miss triggers full-history scan, which finds a hit."""
        from archiver.commoncrawl import CCSnapshot

        worker, _ = _make_worker()
        mock_find.return_value = None
        mock_full.return_value = CCSnapshot(
            url="https://example.com/",
            timestamp="20140101120000",
            crawl_id="CC-MAIN-2014-10",
            filename="x.warc.gz",
            offset=0, length=100, status=200, mime="text/html",
        )
        mock_fetch.return_value = b"<html>old archive from 2014</html>"
        result = await worker._capture_via_commoncrawl("https://example.com/")
        assert b"2014" in result.snapshot_html
        mock_full.assert_awaited_once()

    @patch("archiver.worker.cc_fetch_record_html", new_callable=AsyncMock)
    @patch("archiver.worker.cc_find_snapshot", new_callable=AsyncMock)
    async def test_fetch_error_wrapped(
        self,
        mock_find: AsyncMock,
        mock_fetch: AsyncMock,
    ) -> None:
        from archiver.commoncrawl import CCSnapshot

        worker, _ = _make_worker()
        mock_find.return_value = CCSnapshot(
            url="https://example.com/",
            timestamp="20260101120000",
            crawl_id="CC-MAIN-2026-12",
            filename="x.warc.gz",
            offset=0, length=100, status=200, mime="text/html",
        )
        mock_fetch.side_effect = RuntimeError("range fetch 500")
        import pytest as _pt

        with _pt.raises(CaptureError, match="range-fetch failed"):
            await worker._capture_via_commoncrawl("https://example.com/")


class TestAntibotExhaustion:
    async def test_antibot_on_last_tier_marks_failed(self) -> None:
        """Antibot on the final tier → archive moved to FAILED."""
        worker, mock_conn = _make_worker()
        job = _make_job(tier=CaptureTier.ARCHIVE_TODAY_SUBMIT)
        await worker._handle_antibot(mock_conn, job, "blocked everywhere")
        # Archive must have been marked FAILED.
        calls = worker._archive_repo.update_status.call_args_list
        fail_calls = [
            c for c in calls
            if len(c[0]) >= 3 and c[0][2] == ArchiveStatus.FAILED  # noqa: PLR2004
        ]
        assert len(fail_calls) == 1
        # No escalation enqueue on exhaustion.
        worker._job_repo.enqueue.assert_not_awaited()


class TestCommonCrawlSuccessSource:
    @patch("archiver.worker.save_artifacts", new_callable=AsyncMock)
    @patch("archiver.worker.cc_fetch_record_html", new_callable=AsyncMock)
    @patch("archiver.worker.cc_find_snapshot", new_callable=AsyncMock)
    async def test_commoncrawl_success_sets_source(
        self,
        mock_find: AsyncMock,
        mock_fetch: AsyncMock,
        mock_save: AsyncMock,
    ) -> None:
        """A successful COMMONCRAWL capture records source=commoncrawl."""
        from archiver.commoncrawl import CCSnapshot

        worker, _ = _make_worker()
        job = _make_job(tier=CaptureTier.COMMONCRAWL)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_find.return_value = CCSnapshot(
            url="https://example.com/",
            timestamp="20260101120000",
            crawl_id="CC-MAIN-2026-12",
            filename="x.warc.gz",
            offset=0, length=100, status=200, mime="text/html",
        )
        mock_fetch.return_value = b"<html>cc</html>"
        mock_save.return_value = "x/y"

        await worker._process_job(job)

        # Check source was COMMONCRAWL in update_status kwargs.
        calls = worker._archive_repo.update_status.call_args_list
        complete_calls = [
            c for c in calls
            if len(c[0]) >= 3 and c[0][2] == ArchiveStatus.COMPLETE  # noqa: PLR2004
        ]
        assert any(
            c.kwargs.get("source") == CaptureSource.COMMONCRAWL.value
            for c in complete_calls
        )


class TestCaptureViaArchiveTodaySubmit:
    async def test_no_gate_passer_raises(self) -> None:
        """Submit tier refuses to run without a gate-passing proxy."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(return_value=[])

        import pytest
        with pytest.raises(CaptureError, match="no gate-passing"):
            await worker._capture_via_archive_today_submit(
                "https://example.com/"
            )

    @patch(
        "archiver.worker.fetch_archive_today_snapshot_html",
        new_callable=AsyncMock,
    )
    @patch(
        "archiver.worker.save_to_archive_today", new_callable=AsyncMock
    )
    @patch("camoufox.async_api.AsyncCamoufox")
    async def test_success_path(
        self,
        mock_camoufox_cls: MagicMock,
        mock_save: AsyncMock,
        mock_fetch: AsyncMock,
    ) -> None:
        """Happy path: gate-passer → camoufox submit → fetch → result."""
        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        mock_save.return_value = "https://archive.ph/abc/https://example.com/"
        mock_fetch.return_value = (
            "<html><title>Archived</title><body>content</body></html>"
        )

        # Mock the async context manager returning a browser with
        # new_context / new_page chain returning AsyncMocks.
        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_camoufox_cls.return_value.__aenter__.return_value = mock_browser

        result = await worker._capture_via_archive_today_submit(
            "https://example.com/"
        )

        mock_save.assert_awaited_once()
        mock_fetch.assert_awaited_once()
        assert isinstance(result, CaptureResult)

    @patch("archiver.worker.save_to_archive_today", new_callable=AsyncMock)
    @patch("camoufox.async_api.AsyncCamoufox")
    async def test_submit_returns_none_raises(
        self,
        mock_camoufox_cls: MagicMock,
        mock_save: AsyncMock,
    ) -> None:
        """Submission returned None → CaptureError (escalation or FAIL)."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        mock_save.return_value = None

        mock_page = AsyncMock()
        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_camoufox_cls.return_value.__aenter__.return_value = mock_browser

        import pytest
        with pytest.raises(CaptureError, match="submit failed"):
            await worker._capture_via_archive_today_submit(
                "https://example.com/"
            )


class TestCaptureViaPrivacyFrontend:
    async def test_no_registered_frontend_raises(self) -> None:
        """Unregistered apex → immediate CaptureError → escalation."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        import pytest
        with pytest.raises(CaptureError, match="No privacy frontend"):
            await worker._capture_via_privacy_frontend(
                "https://example.com/article"
            )

    async def test_no_gate_passer_raises(self) -> None:
        """Registered apex but empty proxy pool → CaptureError."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(return_value=[])
        import pytest
        with pytest.raises(CaptureError, match="no gate-passing"):
            await worker._capture_via_privacy_frontend(
                "https://medium.com/@vgr/foo"
            )

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_first_instance_succeeds(
        self, mock_capture: AsyncMock
    ) -> None:
        """Happy path: first instance returns a CaptureResult."""
        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        worker._browser_pool.get_browser = AsyncMock()
        mock_capture.return_value = _make_capture_result()

        result = await worker._capture_via_privacy_frontend(
            "https://medium.com/@vgr/the-gervais-principle"
        )
        assert isinstance(result, CaptureResult)
        mock_capture.assert_awaited_once()
        # URL passed to capture_page should be the scribe-rewritten form
        call_kwargs = mock_capture.call_args.kwargs
        assert call_kwargs["url"].startswith("https://scribe.rip/")

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_falls_through_to_second_instance(
        self, mock_capture: AsyncMock
    ) -> None:
        """First instance raises → try next; win on second."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        worker._browser_pool.get_browser = AsyncMock()
        # Medium registry has two instances — first raises, second ok.
        mock_capture.side_effect = [
            CaptureError("instance 1 down"),
            _make_capture_result(),
        ]
        result = await worker._capture_via_privacy_frontend(
            "https://medium.com/@vgr/foo"
        )
        assert isinstance(result, CaptureResult)
        assert mock_capture.await_count == 2  # noqa: PLR2004

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_all_instances_fail_raises(
        self, mock_capture: AsyncMock
    ) -> None:
        """Every instance raises → bubble CaptureError for tier escalation."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        worker._browser_pool.get_browser = AsyncMock()
        mock_capture.side_effect = CaptureError("dead")

        import pytest
        with pytest.raises(CaptureError, match="All privacy frontend"):
            await worker._capture_via_privacy_frontend(
                "https://medium.com/@vgr/foo"
            )


class TestWorkerShortCircuit:
    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_skips_already_complete_archive(
        self, mock_capture: AsyncMock
    ) -> None:
        """A queued job for an already-COMPLETE archive should no-op."""
        worker, _conn = _make_worker()
        job = _make_job()
        done = _make_archive()
        done = ArchiveRecord(
            id=done.id, url=done.url, url_hash=done.url_hash,
            status=ArchiveStatus.COMPLETE, tier=done.tier, source=done.source,
            created_at=done.created_at,
        )
        worker._archive_repo.get_by_id = AsyncMock(return_value=done)
        worker._browser_pool.get_browser = AsyncMock()

        await worker._process_job(job)

        mock_capture.assert_not_awaited()
        worker._job_repo.complete.assert_awaited_once()


class TestWorkerOnNotify:
    def test_on_notify_does_not_crash(self) -> None:
        worker = Worker(Settings())
        mock_conn = MagicMock(spec=asyncpg.Connection)
        worker._on_notify(mock_conn, 0, "new_job", "job-1")
