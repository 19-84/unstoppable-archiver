# ABOUTME: Unit tests for archive fallback URL construction and toolbar selectors
# ABOUTME: Tests Wayback Machine and archive.today URL prefixes and strip selectors
"""Tests for fallback capture constants."""

from __future__ import annotations

from archiver.fallback import (
    _ARCHIVE_TODAY_STRIP_SELECTORS,
    _ARCHIVE_TODAY_URL_PREFIX,
    _WAYBACK_STRIP_SELECTORS,
    _WAYBACK_URL_PREFIX,
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
