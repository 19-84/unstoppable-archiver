# ABOUTME: Unit tests for the core capture pipeline with mocked Playwright
# ABOUTME: Tests SingleFile injection, screenshot, text extraction, WARC, thumbnails, and error paths
"""Tests for capture pipeline."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Browser, BrowserContext

from archiver.capture import (
    _await_challenge_completion,
    _generate_thumbnail,
    _looks_like_block_page,
    _strip_csp_route,
    capture_page,
    close_context_bounded,
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
    page.wait_for_load_state = AsyncMock()
    page.title = AsyncMock(return_value="Test Page")
    page.evaluate = AsyncMock(
        side_effect=[
            # 1st call: body.innerText for detection
            "Hello world content here " * 50,
            # 2nd call: documentElement.outerHTML for challenge detection
            "<html><body>Hello world content</body></html>",
            # 3rd call: consent-style cleanup (returns None)
            None,
            # 4th call: SingleFile getPageData
            {
                "content": "<html><body>archived</body></html>",
                "title": "Test Page",
            },
            # 5th call: pre-screenshot scroll-through (returns None)
            None,
            # 6th call: body.innerText for text extraction
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


    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_firefox_xray_falls_back_to_script_tag(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Camoufox falls back to script tag when Xray TypedArray error."""
        from archiver.detection import DetectionSignal
        from archiver.enums import CaptureTier

        mock_detect.return_value = DetectionSignal(is_blocked=False)

        page = _make_mock_page()
        page.add_script_tag = AsyncMock()
        page.wait_for_function = AsyncMock()
        # evaluate calls:
        # 1. body.innerText for anti-bot detection
        # 2. documentElement.outerHTML for challenge detection
        # 3. consent-style cleanup
        # 4. SINGLEFILE_CAPTURE_JS raises Xray error
        # 5. read window.__sf_result (from script tag path)
        # 6. pre-screenshot scroll-through
        # 7. body.innerText for text extraction
        page.evaluate = AsyncMock(
            side_effect=[
                "Hello world content here " * 50,
                "<html><body>content</body></html>",
                None,
                Exception("Accessing TypedArray data over Xrays is slow"),
                {"content": "<html>archived</html>", "title": "Test"},
                None,
                "Hello world content here " * 50,
            ]
        )
        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        result = await capture_page(
            "https://example.com", browser, settings,
            tier=CaptureTier.CAMOUFOX,
        )

        assert isinstance(result, CaptureResult)
        # Xray error triggered script tag fallback
        page.add_script_tag.assert_awaited_once()

    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_timeout_detects_antibot(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Navigation timeout checks page for anti-bot markers."""
        from archiver.detection import DetectionSignal

        mock_detect.return_value = DetectionSignal(
            is_blocked=True, reason="just a moment"
        )

        page = _make_mock_page()
        page.goto = AsyncMock(
            side_effect=TimeoutError("navigation timeout")
        )
        # Title without challenge markers so the timeout falls to the
        # plain anti-bot check (not the JS-challenge wait path).
        page.title = AsyncMock(return_value="Access Denied")
        page.evaluate = AsyncMock(return_value="Access denied by server")
        page.wait_for_function = AsyncMock()
        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        with pytest.raises(AntiBotDetectedError, match="timeout"):
            await capture_page(
                "https://example.com", browser, settings
            )


    @patch("archiver.capture.capture_via_cli", new_callable=AsyncMock)
    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_cli_returns_block_page_falls_to_page_content(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        mock_cli: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """CLI's unbranded Chromium sometimes gets a 403 block page;
        should fall through to page.content() (live DOM from stealth browser)."""
        from archiver.detection import DetectionSignal
        from archiver.enums import CaptureTier

        mock_detect.return_value = DetectionSignal(is_blocked=False)
        mock_cli.return_value = (
            "<html><head><title>403 Forbidden</title></head><body>nope</body></html>"
        )

        page = _make_mock_page()
        page.add_script_tag = AsyncMock()
        page.wait_for_function = AsyncMock()
        # page.content() is the ultimate fallback — mock it.
        page.content = AsyncMock(
            return_value="<html><body>live dom content</body></html>"
        )
        # evaluate: body text, outerHTML for challenge, consent cleanup,
        # Xray error (triggers fallback chain), script-tag result error
        # (CLI fires, CLI returns block → page.content fires), then
        # pre-screenshot scroll, body text for indexing.
        page.evaluate = AsyncMock(
            side_effect=[
                "body text",
                "<html><body>content</body></html>",
                None,
                Exception("Accessing TypedArray data over Xrays is slow"),
                {"error": "script tag blocked by CSP"},
                None,
                "body text",
            ]
        )
        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        result = await capture_page(
            "https://example.com", browser, settings,
            tier=CaptureTier.CAMOUFOX,
        )
        assert isinstance(result, CaptureResult)
        page.content.assert_awaited_once()
        assert b"live dom content" in result.snapshot_html

    @patch("archiver.capture.capture_via_cli", new_callable=AsyncMock)
    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_cli_raises_falls_to_page_content(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        mock_cli: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """CLI subprocess raising also falls through to page.content()."""
        from archiver.detection import DetectionSignal
        from archiver.enums import CaptureTier

        mock_detect.return_value = DetectionSignal(is_blocked=False)
        mock_cli.side_effect = CaptureError("CLI crashed")

        page = _make_mock_page()
        page.add_script_tag = AsyncMock()
        page.wait_for_function = AsyncMock()
        page.content = AsyncMock(return_value="<html>live</html>")
        page.evaluate = AsyncMock(
            side_effect=[
                "body text",
                "<html></html>",
                None,
                Exception("Xrays TypedArray"),
                {"error": "script tag blocked"},
                None,
                "body text",
            ]
        )
        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )
        result = await capture_page(
            "https://example.com", browser, settings,
            tier=CaptureTier.CAMOUFOX,
        )
        assert b"live" in result.snapshot_html

    @patch("archiver.capture.capture_via_cli", new_callable=AsyncMock)
    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_firefox_script_tag_error_falls_through_to_cli(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        mock_cli: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Script-tag fallback failure now escalates to CLI subprocess.

        This validates the tier-3 fallback — Firefox's world-isolation
        can defeat both in-browser strategies; the CLI spawns an
        independent Chromium and reliably captures even strict-CSP pages.
        """
        from archiver.detection import DetectionSignal
        from archiver.enums import CaptureTier

        mock_detect.return_value = DetectionSignal(is_blocked=False)
        mock_cli.return_value = "<html>archived via CLI</html>"

        page = _make_mock_page()
        page.add_script_tag = AsyncMock()
        page.wait_for_function = AsyncMock()
        # evaluate: body text, html for challenge detect, consent
        # cleanup, Xray error (triggers fallback), script-tag result
        # (error → CLI fallback fires), then pre-screenshot scroll,
        # then body text for indexing.
        page.evaluate = AsyncMock(
            side_effect=[
                "body text",
                "<html><body>content</body></html>",
                None,
                Exception("Accessing TypedArray data over Xrays is slow"),
                {"error": "SingleFile failed"},
                None,
                "body text",
            ]
        )
        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        result = await capture_page(
            "https://example.com", browser, settings,
            tier=CaptureTier.CAMOUFOX,
        )
        assert isinstance(result, CaptureResult)
        mock_cli.assert_awaited_once()


    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    async def test_post_timeout_page_check_failure(
        self,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """goto succeeds, wait_for_load_state times out,
        then page.title() also fails — covers lines 130-131."""
        page = AsyncMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.wait_for_load_state = AsyncMock(
            side_effect=TimeoutError("networkidle timeout")
        )
        page.title = AsyncMock(
            side_effect=Exception("page crashed")
        )
        page.evaluate = AsyncMock(return_value="")
        page.on = MagicMock()
        page.add_init_script = AsyncMock()

        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        with pytest.raises(CaptureError):
            await capture_page(
                "https://example.com", browser, settings
            )


class TestCloseContextBounded:
    """close_context_bounded must return within its timeout even when
    the underlying context.close() hangs.

    This is the fix for a worker-wedge: capture_page's
    `finally: await context.close()` runs while the worker's
    wait_for(max_capture_timeout) is cancelling a timed-out capture.
    An unbounded close() on a wedged browser blocks that cancellation
    forever — the job stays 'running' and the worker concurrency slot
    is permanently lost (observed live: camoufox_proxy jobs stuck
    'running' 12+ minutes against a 300s timeout)."""

    async def test_returns_even_when_close_hangs(self) -> None:
        """A context.close() that never completes must not block the
        caller past the timeout — the whole point of the bound."""
        import asyncio
        import time

        context = MagicMock(spec=BrowserContext)

        async def _hang() -> None:
            await asyncio.Event().wait()  # never resolves

        context.close = _hang

        start = time.monotonic()
        await close_context_bounded(
            context, url="https://example.com", timeout=0.3,
        )
        elapsed = time.monotonic() - start
        # Returned ~0.3s, NOT hung. Generous ceiling for CI jitter.
        assert elapsed < 3.0, f"close_context_bounded hung: {elapsed:.1f}s"  # noqa: PLR2004

    async def test_completes_normally_for_fast_close(self) -> None:
        """The happy path: a well-behaved close() is awaited to
        completion and not left dangling."""
        context = MagicMock(spec=BrowserContext)
        context.close = AsyncMock()
        await close_context_bounded(
            context, url="https://example.com", timeout=5.0,
        )
        context.close.assert_awaited_once()


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
        """SingleFile returning a dict-shaped but content-less result
        should raise CaptureError('unexpected result') — it shouldn't
        fall through to CLI because this looks like a successful call
        that returned garbage. CLI is only for failure/None cases."""
        from archiver.detection import DetectionSignal

        mock_detect.return_value = DetectionSignal(is_blocked=False)

        page = AsyncMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=200)
        )
        page.wait_for_load_state = AsyncMock()
        page.title = AsyncMock(return_value="Test")
        page.evaluate = AsyncMock(
            side_effect=[
                "body text",                 # body.innerText for detection
                "<html></html>",              # documentElement.outerHTML for challenge
                None,                         # consent-style cleanup
                {"wrong_shape": "no content"},  # SingleFile returned dict without "content"
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
        page.title = AsyncMock(return_value="")
        page.evaluate = AsyncMock(return_value="")
        page.on = MagicMock()
        page.add_init_script = AsyncMock()
        page.wait_for_load_state = AsyncMock()

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


class TestLooksLikeBlockPage:
    def test_title_403_matches(self) -> None:
        assert _looks_like_block_page("<html><head><title>403 Forbidden</title></head>") is True

    def test_access_denied_title_matches(self) -> None:
        assert _looks_like_block_page("<html><title>Access Denied</title>") is True

    def test_normal_article_no_match(self) -> None:
        html = "<html><title>A Very Long Article</title>" + "lorem ipsum " * 1000
        assert _looks_like_block_page(html) is False

    def test_403_deep_in_body_ignored(self) -> None:
        """'403' in a late body section should NOT match — only first 4KB."""
        html = "<html><title>Normal</title>" + "padding " * 600 + "<title>403</title>"
        assert _looks_like_block_page(html) is False


class TestStripCspRoute:
    async def test_non_http_request_continues(self) -> None:
        route = AsyncMock()
        route.request.url = "data:text/html,<b>x</b>"
        route.continue_ = AsyncMock()
        await _strip_csp_route(route)
        route.continue_.assert_awaited_once()

    async def test_strips_csp_header(self) -> None:
        route = AsyncMock()
        route.request.url = "https://example.com/asset.css"
        resp = MagicMock()
        resp.headers = {
            "Content-Security-Policy": "default-src 'self'",
            "X-Frame-Options": "DENY",
        }
        route.fetch = AsyncMock(return_value=resp)
        route.fulfill = AsyncMock()
        await _strip_csp_route(route)
        kwargs = route.fulfill.call_args.kwargs
        headers = kwargs["headers"]
        assert "content-security-policy" not in {k.lower() for k in headers}
        assert "X-Frame-Options" in headers

    async def test_fetch_exception_falls_back_to_continue(self) -> None:
        route = AsyncMock()
        route.request.url = "https://example.com/fail"
        route.fetch = AsyncMock(side_effect=RuntimeError("boom"))
        route.continue_ = AsyncMock()
        await _strip_csp_route(route)
        route.continue_.assert_awaited_once()


class TestAwaitChallengeCompletion:
    async def test_cleared_returns_true(self) -> None:
        page = AsyncMock()
        page.wait_for_function = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        assert await _await_challenge_completion(page, timeout_ms=100) is True

    async def test_timeout_returns_false(self) -> None:
        page = AsyncMock()
        page.wait_for_function = AsyncMock(side_effect=TimeoutError("nope"))
        assert await _await_challenge_completion(page, timeout_ms=100) is False


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
        import zstandard as zstd
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
        # snapshot.html is zstd-compressed at write time; the legacy
        # plain file should NOT be present and the .zst must round-trip
        # back to the exact original bytes.
        assert (out_dir / "snapshot.html.zst").exists()
        assert not (out_dir / "snapshot.html").exists()
        assert (out_dir / "screenshot.png").exists()
        assert (out_dir / "thumbnail.png").exists()
        decompressed = zstd.ZstdDecompressor().decompress(
            (out_dir / "snapshot.html.zst").read_bytes()
        )
        assert decompressed == b"<html>test</html>"

    async def test_compression_ratio_better_than_2x(
        self, tmp_path: Path
    ) -> None:
        """Plain HTML should compress at least 2x -- guards against a
        future regression accidentally writing uncompressed bytes."""
        repetitive_html = (b"<div>hello world</div>" * 1000)
        result = CaptureResult(
            snapshot_html=repetitive_html,
            screenshot_png=_make_tiny_png(),
            thumbnail_png=_make_tiny_png(),
            text_content="",
            title="Test",
            warc_path=None,
            warc_size=0,
            content_hash="abc",
            screenshot_hash="def",
        )
        rel_dir = await save_artifacts(result, "h", tmp_path)
        compressed = (tmp_path / rel_dir / "snapshot.html.zst").stat().st_size
        assert compressed * 2 < len(repetitive_html), (
            f"expected >=2x compression, got "
            f"{len(repetitive_html)} -> {compressed}"
        )

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


class TestCookieCacheIntegration:
    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_cookie_injected_and_extracted(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Cookie cache injects before navigation and extracts after capture."""
        from archiver.cookie_cache import CfClearanceCache
        from archiver.detection import DetectionSignal

        mock_detect.return_value = DetectionSignal(is_blocked=False)

        page = _make_mock_page()
        browser = _make_mock_browser(page)
        context = browser.new_context.return_value
        # Mock cookies() to return a cf_clearance cookie
        context.cookies = AsyncMock(return_value=[
            {"name": "cf_clearance", "value": "token123", "domain": ".example.com", "path": "/"},
        ])
        context.add_cookies = AsyncMock()

        cache = CfClearanceCache()
        cache.put("example.com", "cf_clearance", "old_token")

        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        result = await capture_page(
            "https://example.com", browser, settings,
            cookie_cache=cache,
        )

        assert isinstance(result, CaptureResult)
        # Should have injected the cached cookie
        context.add_cookies.assert_awaited_once()
        # Should have extracted new cookie
        cookie = cache.get("example.com")
        assert cookie is not None
        assert cookie.value == "token123"

    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_no_cookie_cache_skips_injection(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Without cookie_cache param, no injection/extraction occurs."""
        from archiver.detection import DetectionSignal

        mock_detect.return_value = DetectionSignal(is_blocked=False)

        page = _make_mock_page()
        browser = _make_mock_browser(page)
        context = browser.new_context.return_value

        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        result = await capture_page(
            "https://example.com", browser, settings
        )

        assert isinstance(result, CaptureResult)
        context.add_cookies.assert_not_awaited()


class TestCapturePageStripSelectors:
    @patch("archiver.capture.load_bundle", return_value="// fake JS")
    @patch("archiver.capture.check_anti_bot")
    async def test_strip_selectors_invokes_dom_removal(
        self,
        mock_detect: MagicMock,
        mock_bundle: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Pass-through test: strip selectors must run page.evaluate once each."""
        from archiver.detection import DetectionSignal

        mock_detect.return_value = DetectionSignal(is_blocked=False)

        page = AsyncMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.wait_for_load_state = AsyncMock()
        page.title = AsyncMock(return_value="T")
        page.on = MagicMock()
        page.add_init_script = AsyncMock()
        page.screenshot = AsyncMock(return_value=_make_tiny_png())

        # 1st: body text pre-detection
        # 2nd: documentElement.outerHTML for challenge detection
        # 3rd-4th: two strip selectors
        # 5th: consent-style cleanup
        # 6th: SingleFile getPageData
        # 7th: pre-screenshot scroll-through
        # 8th: body text post-capture
        page.evaluate = AsyncMock(
            side_effect=[
                "hello " * 50,
                "<html></html>",
                None,
                None,
                None,
                {"content": "<html></html>", "title": "T"},
                None,
                "hello " * 50,
            ]
        )

        browser = _make_mock_browser(page)
        settings = Settings(
            artifacts_dir=tmp_path,
            singlefile_bundle_path=Path("fake.js"),
        )

        await capture_page(
            "https://archive.today/abc/https://example.com",
            browser,
            settings,
            strip_selectors=["#HEADER", "#DIVSHARE"],
        )

        # Strip selectors were each passed to page.evaluate with the
        # removal script as the first arg.
        eval_calls = page.evaluate.call_args_list
        selector_args = [
            call.args[1] for call in eval_calls if len(call.args) > 1
        ]
        assert "#HEADER" in selector_args
        assert "#DIVSHARE" in selector_args
