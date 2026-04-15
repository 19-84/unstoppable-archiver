# ABOUTME: Tests for cf_clearance cookie cache
# ABOUTME: Covers put/get, TTL expiry, domain normalization, URL lookup
"""Tests for cookie_cache module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import time_machine

from archiver.cookie_cache import CfClearanceCache, _normalize_domain


class TestNormalizeDomain:
    def test_strips_www(self) -> None:
        assert _normalize_domain("www.example.com") == "example.com"

    def test_keeps_non_www(self) -> None:
        assert _normalize_domain("example.com") == "example.com"

    def test_keeps_subdomain(self) -> None:
        assert _normalize_domain("sub.example.com") == "sub.example.com"


class TestCfClearanceCache:
    def test_put_and_get(self) -> None:
        cache = CfClearanceCache()
        cache.put("example.com", "cf_clearance", "abc123")
        cookie = cache.get("example.com")
        assert cookie is not None
        assert cookie.value == "abc123"

    def test_get_missing_returns_none(self) -> None:
        cache = CfClearanceCache()
        assert cache.get("example.com") is None

    def test_expired_returns_none(self) -> None:
        cache = CfClearanceCache(ttl_seconds=1)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        with time_machine.travel(now):
            cache.put("example.com", "cf_clearance", "abc")
        with time_machine.travel(now + timedelta(seconds=5)):
            assert cache.get("example.com") is None

    def test_www_normalization(self) -> None:
        cache = CfClearanceCache()
        cache.put("www.example.com", "cf_clearance", "abc")
        assert cache.get("example.com") is not None

    def test_get_for_url(self) -> None:
        cache = CfClearanceCache()
        cache.put("example.com", "cf_clearance", "token")
        cookie = cache.get_for_url("https://www.example.com/page")
        assert cookie is not None
        assert cookie.value == "token"

    def test_get_for_url_missing(self) -> None:
        cache = CfClearanceCache()
        assert cache.get_for_url("https://other.com") is None
