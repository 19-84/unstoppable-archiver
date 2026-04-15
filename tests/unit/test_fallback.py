# ABOUTME: Unit tests for archive fallback — availability checks and browser capture
# ABOUTME: Tests Wayback Machine and archive.today API checks, toolbar stripping, error paths
"""Tests for fallback capture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import respx

from archiver.fallback import (
    _ARCHIVE_TODAY_STRIP_SELECTORS,
    _ARCHIVE_TODAY_URL_PREFIX,
    _WAYBACK_STRIP_SELECTORS,
    _WAYBACK_URL_PREFIX,
    capture_from_archive_today,
    capture_from_wayback,
    check_archive_today_availability,
    check_wayback_availability,
)


class TestFallbackConstants:
    def test_wayback_url_prefix(self) -> None:
        url = "https://example.com/page"
        full = _WAYBACK_URL_PREFIX + url
        assert full.startswith("https://web.archive.org/web/2/")
        assert full.endswith(url)

    def test_archive_today_url_prefix(self) -> None:
        url = "https://example.com/page"
        full = _ARCHIVE_TODAY_URL_PREFIX + url
        assert full.startswith("https://archive.today/newest/")
        assert full.endswith(url)

    def test_wayback_strip_selectors_not_empty(self) -> None:
        assert len(_WAYBACK_STRIP_SELECTORS) > 0

    def test_archive_today_strip_selectors_not_empty(
        self,
    ) -> None:
        assert len(_ARCHIVE_TODAY_STRIP_SELECTORS) > 0

    def test_wayback_strips_toolbar(self) -> None:
        assert "#wm-ib-bar" in _WAYBACK_STRIP_SELECTORS

    def test_archive_today_strips_header(self) -> None:
        assert "#HEADER" in _ARCHIVE_TODAY_STRIP_SELECTORS


class TestWaybackAvailability:
    @respx.mock
    async def test_available(self) -> None:
        respx.get("https://archive.org/wayback/available").mock(
            return_value=httpx.Response(
                200,
                json={
                    "archived_snapshots": {
                        "closest": {
                            "available": True,
                            "url": "https://web.archive.org/web/2024/https://example.com",
                        }
                    }
                },
            )
        )
        result = await check_wayback_availability("https://example.com")
        assert result is not None
        assert "web.archive.org" in result

    @respx.mock
    async def test_not_available(self) -> None:
        respx.get("https://archive.org/wayback/available").mock(
            return_value=httpx.Response(
                200, json={"archived_snapshots": {}}
            )
        )
        result = await check_wayback_availability("https://example.com")
        assert result is None

    @respx.mock
    async def test_api_error(self) -> None:
        respx.get("https://archive.org/wayback/available").mock(
            return_value=httpx.Response(500)
        )
        result = await check_wayback_availability("https://example.com")
        assert result is None


class TestArchiveTodayAvailability:
    @respx.mock
    async def test_available(self) -> None:
        respx.head("https://archive.today/newest/https://example.com").mock(
            return_value=httpx.Response(200)
        )
        result = await check_archive_today_availability("https://example.com")
        assert result is True

    @respx.mock
    async def test_not_available_redirect(self) -> None:
        respx.head("https://archive.today/newest/https://example.com").mock(
            return_value=httpx.Response(302)
        )
        result = await check_archive_today_availability("https://example.com")
        assert result is False


class TestCaptureFromWayback:
    async def test_not_found_returns_false(self) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=404)
        )
        result = await capture_from_wayback(
            "https://example.com", page
        )
        assert result is False

    async def test_not_archived_marker_returns_false(self) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=200)
        )
        page.title = AsyncMock(return_value="")
        page.evaluate = AsyncMock(
            return_value="Wayback Machine has not archived that URL"
        )
        result = await capture_from_wayback(
            "https://example.com", page
        )
        assert result is False

    async def test_success_strips_toolbar(self) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=200)
        )
        page.title = AsyncMock(return_value="Example")
        page.evaluate = AsyncMock(return_value="Real content")

        result = await capture_from_wayback(
            "https://example.com", page
        )
        assert result is True
        # body check + 6 strip selectors = 7 evaluate calls
        assert page.evaluate.call_count >= 7  # noqa: PLR2004


class TestCaptureFromArchiveToday:
    async def test_not_found_returns_false(self) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=404)
        )
        result = await capture_from_archive_today(
            "https://example.com", page
        )
        assert result is False

    async def test_redirect_to_homepage_returns_false(self) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=200)
        )
        page.url = "https://archive.today/"
        result = await capture_from_archive_today(
            "https://example.com", page
        )
        assert result is False

    async def test_success_strips_toolbar(self) -> None:
        page = AsyncMock()
        page.goto = AsyncMock(
            return_value=MagicMock(status=200)
        )
        page.url = "https://archive.today/2024/https://example.com"
        page.evaluate = AsyncMock()

        result = await capture_from_archive_today(
            "https://example.com", page
        )
        assert result is True
