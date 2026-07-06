# ABOUTME: Unit tests for worker tier escalation and job processing logic
# ABOUTME: Tests next_tier, process_job success/failure/antibot paths with mocked deps
"""Tests for worker logic."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

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
    frontend_status_repo = AsyncMock()
    # Default: one verified instance per apex so happy-path tests don't
    # need to mock it. Tests exercising the empty-pool path override this.
    frontend_status_repo.list_passing = AsyncMock(
        return_value=["https://scribe.rip"]
    )
    worker._frontend_status_repo = frontend_status_repo
    return worker, mock_conn


@pytest.fixture(autouse=True)
def _capture_time_safety_allows(  # type: ignore[misc]
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncMock:
    """Stub the capture-time SSRF re-check so unit tests never resolve DNS.

    Individual tests override the return value to exercise the block path.
    """
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "archiver.worker.check_url_safety_async", mock
    )
    return mock


class TestCaptureTimeSafety:
    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_unsafe_url_fails_without_escalation(
        self,
        mock_capture: AsyncMock,
        _capture_time_safety_allows: AsyncMock,
    ) -> None:
        worker, _conn = _make_worker()
        job = _make_job(tier=CaptureTier.CHROMIUM)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        _capture_time_safety_allows.return_value = (
            "Blocked private/internal IP: 10.0.0.5"
        )

        await worker._process_job(job)

        mock_capture.assert_not_awaited()
        worker._job_repo.enqueue.assert_not_awaited()
        fail_kwargs = worker._job_repo.fail.call_args
        assert fail_kwargs.kwargs.get("retry") is False
        status_call = worker._archive_repo.update_status.call_args
        assert status_call[0][2] == ArchiveStatus.FAILED
        assert "safety check" in status_call.kwargs["error_message"]

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_safe_url_proceeds_to_capture(
        self,
        mock_capture: AsyncMock,
        _capture_time_safety_allows: AsyncMock,
    ) -> None:
        worker, _conn = _make_worker()
        job = _make_job(tier=CaptureTier.CHROMIUM)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_capture.return_value = _make_capture_result()
        worker._browser_pool.get_browser = AsyncMock(
            return_value=AsyncMock()
        )
        with patch(
            "archiver.worker.save_artifacts",
            new_callable=AsyncMock,
            return_value="abc/1",
        ):
            await worker._process_job(job)

        _capture_time_safety_allows.assert_awaited_once()
        worker._job_repo.complete.assert_awaited_once()

    @patch("archiver.worker.check_wayback_availability", new_callable=AsyncMock)
    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    @patch("archiver.worker.save_artifacts", new_callable=AsyncMock, return_value="abc/1")
    async def test_fallback_tier_skips_recheck(
        self,
        _mock_save: AsyncMock,
        mock_capture: AsyncMock,
        mock_availability: AsyncMock,
        _capture_time_safety_allows: AsyncMock,
    ) -> None:
        """Wayback fetches from archive.org, not the submitted URL —
        the capture-time re-check must not run for fallback tiers."""
        worker, _conn = _make_worker()
        job = _make_job(tier=CaptureTier.WAYBACK)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        mock_availability.return_value = (
            "https://web.archive.org/web/20260101000000/https://example.com"
        )
        mock_capture.return_value = _make_capture_result()
        worker._browser_pool.get_browser = AsyncMock(
            return_value=AsyncMock()
        )

        await worker._process_job(job)

        _capture_time_safety_allows.assert_not_awaited()
        worker._job_repo.complete.assert_awaited_once()


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

    def test_commoncrawl_escalates_to_memento(self) -> None:
        assert (
            next_tier(CaptureTier.COMMONCRAWL)
            == CaptureTier.MEMENTO
        )

    def test_memento_escalates_to_archive_today_submit(self) -> None:
        assert (
            next_tier(CaptureTier.MEMENTO)
            == CaptureTier.ARCHIVE_TODAY_SUBMIT
        )

    def test_archive_today_submit_returns_none(self) -> None:
        assert next_tier(CaptureTier.ARCHIVE_TODAY_SUBMIT) is None

    def test_full_chain_has_nine_tiers(self) -> None:
        tiers: list[CaptureTier] = [CaptureTier.CHROMIUM]
        tier: CaptureTier | None = CaptureTier.CHROMIUM
        while (tier := next_tier(tier)) is not None:
            tiers.append(tier)
        total_tiers = 9
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

    @patch("archiver.worker.save_artifacts", new_callable=AsyncMock)
    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    @patch("archiver.worker.fetch_archive_today_snapshot_html", new_callable=AsyncMock)
    @patch("archiver.worker.find_archive_today_snapshot", new_callable=AsyncMock)
    async def test_archive_today_browser_fallback_preserves_source_url(
        self,
        mock_find: AsyncMock,
        mock_fetch: AsyncMock,
        mock_capture: AsyncMock,
        mock_save: AsyncMock,
    ) -> None:
        _ = mock_save  # required by the patch stack, not invoked here
        """When archive.today direct-fetch fails and the worker falls
        through to a full Camoufox render of the memento URL, the
        returned CaptureResult must carry source_url=memento_url. The
        sibling direct-fetch path already set this; the browser
        branch silently dropped it, which meant the detail page's
        'Captured from' provenance block would disappear for the
        exact subset of archive.today captures that needed a browser
        render to bypass CF."""
        worker, _ = _make_worker()
        memento = "https://archive.today/abc/foo"
        mock_find.return_value = memento
        mock_fetch.return_value = None  # CF block forces browser path
        # capture_page returns a result without source_url set —
        # the wrapper layer must fill it in. _make_capture_result()
        # defaults source_url=None so the bare assignment is the
        # 'unset' case we want to validate gets populated.
        mock_capture.return_value = _make_capture_result()
        worker._browser_pool.get_browser = AsyncMock()

        result = await worker._capture_via_archive_today(
            "https://twitter.com/foo",
        )
        assert result.source_url == memento

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
        # The CDX record's crawl time must be persisted structurally,
        # not just buried inside metadata.source_url.
        assert any(
            c.kwargs.get("snapshot_timestamp")
            == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
            for c in complete_calls
        )


class TestCaptureViaMemento:
    @patch("archiver.worker.save_artifacts", new_callable=AsyncMock)
    @patch("archiver.worker.fetch_memento_html", new_callable=AsyncMock)
    @patch("archiver.worker.find_latest_memento", new_callable=AsyncMock)
    async def test_memento_success_sets_source_and_timestamp(
        self,
        mock_find: AsyncMock,
        mock_fetch: AsyncMock,
        mock_save: AsyncMock,
    ) -> None:
        from archiver.memento import MementoHit

        worker, _ = _make_worker()
        job = _make_job(tier=CaptureTier.MEMENTO)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        ts = datetime(2018, 3, 4, 5, 6, 7, tzinfo=UTC)
        mock_find.return_value = MementoHit(
            archive_id="arquivo.pt",
            memento_url=(
                "https://arquivo.pt/wayback/20180304050607/"
                "https://example.com"
            ),
            timestamp=ts,
        )
        mock_fetch.return_value = "<html>from arquivo</html>"
        mock_save.return_value = "x/y"

        await worker._process_job(job)

        calls = worker._archive_repo.update_status.call_args_list
        complete_calls = [
            c for c in calls
            if len(c[0]) >= 3 and c[0][2] == ArchiveStatus.COMPLETE  # noqa: PLR2004
        ]
        assert any(
            c.kwargs.get("source") == CaptureSource.MEMENTO.value
            for c in complete_calls
        )
        assert any(
            c.kwargs.get("snapshot_timestamp") == ts
            for c in complete_calls
        )
        assert any(
            "arquivo.pt" in (c.kwargs.get("metadata") or "")
            for c in complete_calls
        )

    @patch("archiver.worker.find_latest_memento", new_callable=AsyncMock)
    async def test_no_memento_raises(self, mock_find: AsyncMock) -> None:
        worker, _ = _make_worker()
        mock_find.return_value = None
        with pytest.raises(CaptureError, match="No memento"):
            await worker._capture_via_memento("https://example.com/")

    @patch("archiver.worker.fetch_memento_html", new_callable=AsyncMock)
    @patch("archiver.worker.find_latest_memento", new_callable=AsyncMock)
    async def test_memento_fetch_failure_raises(
        self, mock_find: AsyncMock, mock_fetch: AsyncMock
    ) -> None:
        from archiver.memento import MementoHit

        worker, _ = _make_worker()
        mock_find.return_value = MementoHit(
            archive_id="ukwa",
            memento_url="https://www.webarchive.org.uk/wayback/x",
            timestamp=None,
        )
        mock_fetch.return_value = None
        with pytest.raises(CaptureError, match="fetch failed"):
            await worker._capture_via_memento("https://example.com/")


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

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_no_gate_passer_falls_through_to_direct(
        self, mock_capture: AsyncMock,
    ) -> None:
        """Empty SOCKS5 pool no longer fatal — most frontends are
        Anubis-walled and Camoufox solves those direct. The capture
        call goes through with proxy=None; CF-walled instances still
        fail at navigation and the loop moves on."""
        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(return_value=[])
        worker._frontend_status_repo.list_passing = AsyncMock(
            return_value=["https://scribe.rip"],
        )
        worker._browser_pool.get_browser = AsyncMock(return_value=AsyncMock())
        mock_capture.return_value = _make_capture_result()
        result = await worker._capture_via_privacy_frontend(
            "https://medium.com/@vgr/foo",
        )
        assert result is not None
        # Critical: capture_page must have been called WITHOUT a proxy
        # so the direct-Camoufox path actually runs.
        assert mock_capture.await_count == 1
        assert mock_capture.await_args is not None
        _, kwargs = mock_capture.await_args
        assert kwargs.get("proxy") is None

    async def test_no_verified_frontend_raises(self) -> None:
        """Registered apex, gate-passer exists, but no probe-verified
        instance yet → CaptureError so tier escalates to wayback cleanly
        rather than storing a challenge page as content."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        worker._frontend_status_repo.list_passing = AsyncMock(return_value=[])
        import pytest
        with pytest.raises(CaptureError, match="no content-verified"):
            await worker._capture_via_privacy_frontend(
                "https://medium.com/@vgr/foo"
            )

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_first_verified_instance_succeeds(
        self, mock_capture: AsyncMock
    ) -> None:
        """Happy path: first verified instance returns a CaptureResult."""
        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        worker._frontend_status_repo.list_passing = AsyncMock(
            return_value=["https://scribe.rip"]
        )
        worker._browser_pool.get_browser = AsyncMock()
        mock_capture.return_value = _make_capture_result()

        result = await worker._capture_via_privacy_frontend(
            "https://medium.com/@vgr/the-gervais-principle"
        )
        assert isinstance(result, CaptureResult)
        mock_capture.assert_awaited_once()
        call_kwargs = mock_capture.call_args.kwargs
        assert call_kwargs["url"].startswith("https://scribe.rip/")

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_falls_through_to_second_verified_instance(
        self, mock_capture: AsyncMock
    ) -> None:
        """First verified instance raises → try next; win on second."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        worker._frontend_status_repo.list_passing = AsyncMock(
            return_value=[
                "https://scribe.rip",
                "https://libmedium.batsense.net",
            ]
        )
        worker._browser_pool.get_browser = AsyncMock()
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
    async def test_not_found_marker_skips_instance(
        self, mock_capture: AsyncMock
    ) -> None:
        """Probe-verified instance returning a not-found shell is treated
        as a per-URL miss — try the next instance, don't store the shell."""
        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        worker._frontend_status_repo.list_passing = AsyncMock(
            return_value=[
                "https://scribe.rip",
                "https://libmedium.batsense.net",
            ]
        )
        worker._browser_pool.get_browser = AsyncMock()
        # First instance returns the not-found shell — second has it.
        good = _make_capture_result()
        from dataclasses import replace
        bad = replace(
            good,
            snapshot_html=b"<html><body>This article is missing</body></html>",
        )
        mock_capture.side_effect = [bad, good]

        result = await worker._capture_via_privacy_frontend(
            "https://medium.com/@vgr/foo"
        )
        # Got the second instance's bytes — first was rejected. Identity
        # check would fail because the privacy_frontend path wraps the
        # winning result in dataclasses.replace() to record source_url.
        assert result.snapshot_html == good.snapshot_html
        assert result.content_hash == good.content_hash
        # source_url provenance landed: it points at the SECOND instance
        # (the one whose bytes we kept), not the rejected first.
        assert result.source_url is not None
        assert "scribe.rip" not in result.source_url
        assert mock_capture.await_count == 2  # noqa: PLR2004

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_all_verified_instances_fail_raises(
        self, mock_capture: AsyncMock
    ) -> None:
        """Every verified instance raises → escalation CaptureError."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=["socks5://1.2.3.4:1080"]
        )
        worker._frontend_status_repo.list_passing = AsyncMock(
            return_value=["https://scribe.rip"]
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


class TestGateProbeBatch:
    """Cover the SOCKS5 gate-probe path that refills proxy_status."""

    @staticmethod
    def _socks_proxy(server: str):
        from archiver.proxy import ProxyConfig
        return ProxyConfig(server=server)

    async def test_empty_rotator_returns_early(self) -> None:
        worker, _ = _make_worker()
        # _proxy_rotator.proxies defaults to () for a fresh ProxyRotator.
        await worker._gate_probe_batch(batch_size=5)
        # Nothing got recorded.
        worker._proxy_status_repo.record.assert_not_awaited()

    @patch("archiver.worker.filter_socks5")
    async def test_no_socks_returns_early(
        self, mock_filter_socks5: MagicMock,
    ) -> None:
        worker, _ = _make_worker()
        worker._proxy_rotator.proxies = (self._socks_proxy("http://1.2.3.4:8080"),)
        mock_filter_socks5.return_value = []
        await worker._gate_probe_batch(batch_size=5)
        worker._proxy_status_repo.record.assert_not_awaited()

    @patch("archiver.worker.filter_gate_passing", new_callable=AsyncMock)
    @patch("archiver.worker.filter_by_asn", new_callable=AsyncMock)
    @patch("archiver.worker.filter_socks5")
    async def test_already_passing_candidates_skipped(
        self,
        mock_filter_socks5: MagicMock,
        mock_filter_by_asn: AsyncMock,
        mock_filter_gate: AsyncMock,
    ) -> None:
        """If every healthy SOCKS5 is already on the passing list, no probe."""
        proxy = self._socks_proxy("socks5://1.2.3.4:1080")
        worker, _ = _make_worker()
        worker._proxy_rotator.proxies = (proxy,)
        mock_filter_socks5.return_value = [proxy]
        # Existing passing list contains this proxy.
        worker._proxy_status_repo.list_passing = AsyncMock(
            return_value=[proxy.server],
        )

        await worker._gate_probe_batch(batch_size=5)

        # ASN filter and gate probe should never have been called.
        mock_filter_by_asn.assert_not_awaited()
        mock_filter_gate.assert_not_awaited()
        worker._proxy_status_repo.record.assert_not_awaited()

    @patch("archiver.worker.filter_gate_passing", new_callable=AsyncMock)
    @patch("archiver.worker.filter_by_asn", new_callable=AsyncMock)
    @patch("archiver.worker.filter_socks5")
    async def test_unfiltered_fallback_when_consumer_empty(
        self,
        mock_filter_socks5: MagicMock,
        mock_filter_by_asn: AsyncMock,
        mock_filter_gate: AsyncMock,
    ) -> None:
        """ASN filter empty → fall back to unfiltered candidates."""
        proxies = [self._socks_proxy(f"socks5://1.2.3.{i}:1080") for i in range(3)]
        worker, _ = _make_worker()
        worker._proxy_rotator.proxies = tuple(proxies)
        mock_filter_socks5.return_value = proxies
        mock_filter_by_asn.return_value = []   # no consumer-ASN passers
        # One of the three passed the gate.
        mock_filter_gate.return_value = [proxies[1]]

        await worker._gate_probe_batch(batch_size=10)

        expected_recorded = 3
        # All three got recorded — gate_passing True for one, False for two.
        assert worker._proxy_status_repo.record.await_count == expected_recorded
        record_calls = worker._proxy_status_repo.record.await_args_list
        # gate_passing kw is True for exactly one record call.
        passing_count = sum(
            1 for c in record_calls if c.kwargs.get("gate_passing") is True
        )
        assert passing_count == 1

    @patch("archiver.worker.filter_gate_passing", new_callable=AsyncMock)
    @patch("archiver.worker.filter_by_asn", new_callable=AsyncMock)
    @patch("archiver.worker.filter_socks5")
    async def test_consumer_subset_used_when_present(
        self,
        mock_filter_socks5: MagicMock,
        mock_filter_by_asn: AsyncMock,
        mock_filter_gate: AsyncMock,
    ) -> None:
        """Consumer-ASN subset preferred over datacenter sample."""
        all_proxies = [
            self._socks_proxy(f"socks5://1.2.3.{i}:1080") for i in range(5)
        ]
        consumer_subset = all_proxies[:2]  # first two flagged consumer
        worker, _ = _make_worker()
        worker._proxy_rotator.proxies = tuple(all_proxies)
        mock_filter_socks5.return_value = all_proxies
        mock_filter_by_asn.return_value = consumer_subset
        mock_filter_gate.return_value = consumer_subset[:1]

        await worker._gate_probe_batch(batch_size=5)

        expected_consumer_recorded = 2
        # Only consumer proxies should have been gate-probed and recorded.
        assert (
            worker._proxy_status_repo.record.await_count
            == expected_consumer_recorded
        )
        recorded_servers = {
            c.args[1] for c in worker._proxy_status_repo.record.await_args_list
        }
        assert recorded_servers == {p.server for p in consumer_subset}


class TestProcessJobDispatch:
    """Cover the _dispatch() dispatcher inside _process_job_inner, which the
    unit-level _capture_via_X tests bypass by calling the methods directly."""

    @patch("archiver.worker.save_artifacts", new_callable=AsyncMock,
           return_value="ar/20260514")
    async def test_privacy_frontend_tier_dispatch_sets_source(
        self, _mock_save: AsyncMock,
    ) -> None:
        """job.tier=PRIVACY_FRONTEND routes to _capture_via_privacy_frontend
        and the COMPLETE row gets source=PRIVACY_FRONTEND."""
        worker, _ = _make_worker()
        job = _make_job(tier=CaptureTier.PRIVACY_FRONTEND)
        worker._archive_repo.get_by_id = AsyncMock(
            return_value=_make_archive()
        )
        worker._capture_via_privacy_frontend = AsyncMock(
            return_value=_make_capture_result(),
        )
        await worker._process_job(job)

        # update_status called with source=PRIVACY_FRONTEND
        call_kwargs = worker._archive_repo.update_status.await_args.kwargs
        assert call_kwargs.get("source") == CaptureSource.PRIVACY_FRONTEND


class TestCaptureViaWaybackErrors:
    """Cover wayback's not-found and SPN-fail branches."""

    @patch("archiver.worker.save_to_wayback", new_callable=AsyncMock)
    @patch("archiver.worker.check_wayback_availability", new_callable=AsyncMock)
    async def test_spn_returns_none_raises(
        self,
        mock_check: AsyncMock,
        mock_save: AsyncMock,
    ) -> None:
        """URL missing from Wayback + SPN submission failed → CaptureError."""
        from archiver.errors import CaptureError

        worker, _ = _make_worker()
        mock_check.return_value = None       # not in Wayback
        mock_save.return_value = None        # SPN failed
        worker._browser_pool.get_browser = AsyncMock(return_value=AsyncMock())

        with pytest.raises(CaptureError, match="not in Wayback"):
            await worker._capture_via_wayback("https://example.com")


class TestCaptureViaArchiveTodaySubmitErrors:
    async def test_no_proxy_raises_capture_error(self) -> None:
        """Empty SOCKS5 pool → CaptureError before Camoufox launch."""
        worker, _ = _make_worker()
        # _proxy_status_repo.list_passing returns [] by default in _make_worker.
        with pytest.raises(CaptureError, match="no gate-passing proxy"):
            await worker._capture_via_archive_today_submit(
                "https://example.com",
            )


class TestCapturePageWithProxyRetry:
    """Cover the proxy-mark-failed branch in _capture_page_with_proxy_retry."""

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_failure_with_proxy_marks_failed(
        self, mock_capture: AsyncMock,
    ) -> None:
        """When capture raises and a proxy was passed in, the rotator
        records the failure so subsequent jobs don't re-try the same
        dead proxy. The exception still propagates."""
        from archiver.proxy import ProxyConfig
        worker, _ = _make_worker()
        job = _make_job(tier=CaptureTier.CAMOUFOX_PROXY)
        proxy = ProxyConfig(server="socks5://1.2.3.4:1080")
        worker._proxy_rotator = MagicMock()
        worker._proxy_rotator.mark_failed = MagicMock()
        mock_capture.side_effect = RuntimeError("upstream blew up")

        browser = AsyncMock()
        with pytest.raises(RuntimeError, match="upstream blew up"):
            await worker._capture_page_with_proxy_retry(
                job, "https://example.com", browser, proxy,
            )
        worker._proxy_rotator.mark_failed.assert_called_once_with(proxy)

    @patch("archiver.worker.capture_page", new_callable=AsyncMock)
    async def test_failure_without_proxy_does_not_mark(
        self, mock_capture: AsyncMock,
    ) -> None:
        """No proxy passed → mark_failed must NOT be called."""
        worker, _ = _make_worker()
        job = _make_job(tier=CaptureTier.CAMOUFOX)
        worker._proxy_rotator = MagicMock()
        worker._proxy_rotator.mark_failed = MagicMock()
        mock_capture.side_effect = RuntimeError("nope")

        browser = AsyncMock()
        with pytest.raises(RuntimeError):
            await worker._capture_page_with_proxy_retry(
                job, "https://example.com", browser, None,
            )
        worker._proxy_rotator.mark_failed.assert_not_called()
