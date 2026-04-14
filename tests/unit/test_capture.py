# ABOUTME: Unit tests for the core capture pipeline with mocked Playwright
# ABOUTME: Tests SingleFile injection, screenshot, text extraction, WARC, thumbnails, and error paths
"""Tests for capture pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Browser

from archiver.capture import (
    _generate_thumbnail,
    capture_page,
    save_artifacts,
)
from archiver.config import Settings
from archiver.errors import AntiBotDetectedError, CaptureError
from archiver.models import CaptureResult


def _make_mock_page() -> AsyncMock:
    """Create a mock Playwright Page with standard responses."""
    page = AsyncMock()
    page.goto = AsyncMock(
        return_value=MagicMock(status=200)
    )
    page.title = AsyncMock(return_value="Test Page")
    page.evaluate = AsyncMock(
        side_effect=[
            # First call: body.innerText for detection
            "Hello world content here " * 50,
            # Second call: SingleFile getPageData
            {
                "content": "<html><body>archived</body></html>",
                "title": "Test Page",
            },
            # Third call: body.innerText for text extraction
            "Hello world content here " * 50,
        ]
    )
    # Valid 1x1 PNG
    page.screenshot = AsyncMock(
        return_value=_make_tiny_png()
    )
    page.on = MagicMock()
    page.add_init_script = AsyncMock()
    return page


def _make_tiny_png() -> bytes:
    """Create a minimal valid PNG."""
    from io import BytesIO

    from PIL import Image

    img = Image.new("RGB", (100, 80), color="blue")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_mock_browser(page: AsyncMock) -> MagicMock:
    """Create a mock Browser that passes beartype isinstance check."""
    context = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()

    browser = MagicMock(spec=Browser)
    browser.new_context = AsyncMock(return_value=context)
    return browser


class TestCapturePageSuccess:
    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_returns_capture_result(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        from archiver.detection import DetectionSignal

        mock_detect.return_value = DetectionSignal(is_blocked=False)

        page = _make_mock_page()
        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        result = await capture_page(
            "https://example.com", browser, settings
        )

        assert isinstance(result, CaptureResult)
        assert result.title == "Test Page"
        assert len(result.snapshot_html) > 0
        assert len(result.screenshot_png) > 0
        assert len(result.thumbnail_png) > 0
        assert len(result.content_hash) == 64  # noqa: PLR2004
        assert len(result.screenshot_hash) == 64  # noqa: PLR2004


class TestCapturePageErrors:
    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_antibot_raises_not_wrapped(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        from archiver.detection import DetectionSignal

        mock_detect.return_value = DetectionSignal(
            is_blocked=True, reason="403"
        )

        page = _make_mock_page()
        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        with pytest.raises(AntiBotDetectedError):
            await capture_page(
                "https://example.com", browser, settings
            )

    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_singlefile_bad_result_raises(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        from archiver.detection import DetectionSignal

        mock_detect.return_value = DetectionSignal(is_blocked=False)

        page = AsyncMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=200)
        )
        page.title = AsyncMock(return_value="Test")
        page.evaluate = AsyncMock(
            side_effect=[
                "body text",
                None,  # SingleFile returns None
            ]
        )
        page.on = MagicMock()
        page.add_init_script = AsyncMock()

        browser = _make_mock_browser(page)

        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        with pytest.raises(CaptureError, match="unexpected"):
            await capture_page(
                "https://example.com", browser, settings
            )

    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    async def test_navigation_error_raises_capture_error(
        self,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(
            side_effect=TimeoutError("page timeout")
        )
        page.on = MagicMock()
        page.add_init_script = AsyncMock()

        browser = _make_mock_browser(page)
        context = browser.new_context.return_value

        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        with pytest.raises(CaptureError):
            await capture_page(
                "https://example.com", browser, settings
            )

        # Context should be closed even on error
        context.close.assert_awaited_once()


class TestGenerateThumbnail:
    def test_resizes_image(self) -> None:
        from io import BytesIO

        from PIL import Image

        png = _make_tiny_png()
        thumb = _generate_thumbnail(png, 50, 40)

        img = Image.open(BytesIO(thumb))
        assert img.width <= 50  # noqa: PLR2004
        assert img.height <= 40  # noqa: PLR2004

    def test_output_is_png(self) -> None:
        png = _make_tiny_png()
        thumb = _generate_thumbnail(png, 50, 40)
        assert thumb[:4] == b"\x89PNG"


class TestSaveArtifacts:
    async def test_creates_directory_and_files(
        self, tmp_path: Path
    ) -> None:
        result = CaptureResult(
            snapshot_html=b"<html>test</html>",
            screenshot_png=_make_tiny_png(),
            thumbnail_png=_make_tiny_png(),
            text_content="test text",
            title="Test",
            warc_path=None,
            warc_size=0,
            content_hash="abc123",
            screenshot_hash="def456",
        )

        rel_dir = await save_artifacts(
            result, "urlhash123", tmp_path
        )

        out_dir = tmp_path / rel_dir
        assert (out_dir / "snapshot.html").exists()
        assert (out_dir / "screenshot.png").exists()
        assert (out_dir / "thumbnail.png").exists()
        assert (out_dir / "snapshot.html").read_bytes() == b"<html>test</html>"

    async def test_moves_warc_file(
        self, tmp_path: Path
    ) -> None:
        warc_tmp = tmp_path / "temp.warc.gz"
        warc_tmp.write_bytes(b"fake warc data")

        result = CaptureResult(
            snapshot_html=b"<html>test</html>",
            screenshot_png=_make_tiny_png(),
            thumbnail_png=_make_tiny_png(),
            text_content="test text",
            title="Test",
            warc_path=warc_tmp,
            warc_size=14,
            content_hash="abc",
            screenshot_hash="def",
        )

        rel_dir = await save_artifacts(
            result, "urlhash", tmp_path
        )
        out_dir = tmp_path / rel_dir
        assert (out_dir / "archive.warc.gz").exists()
        assert not warc_tmp.exists()  # moved, not copied

    async def test_returns_relative_path(
        self, tmp_path: Path
    ) -> None:
        result = CaptureResult(
            snapshot_html=b"<html>test</html>",
            screenshot_png=_make_tiny_png(),
            thumbnail_png=_make_tiny_png(),
            text_content="test",
            title="Test",
            warc_path=None,
            warc_size=0,
            content_hash="abc",
            screenshot_hash="def",
        )

        rel_dir = await save_artifacts(
            result, "myhash", tmp_path
        )
        assert rel_dir.startswith("myhash/")
        assert "/" in rel_dir
