# ABOUTME: Tests for in-memory sliding-window rate limiter
# ABOUTME: Covers limit enforcement, retry-after calc, disabled mode, disabled IPs
"""Tests for rate_limit module."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from archiver.config import Settings
from archiver.rate_limit import RateLimiter, enforce_limit


class TestRateLimiter:
    def test_under_limit_allows(self) -> None:
        rl = RateLimiter()
        for _ in range(5):
            allowed, _ = rl.check("1.1.1.1", limit=10)
            assert allowed is True

    def test_over_limit_denies(self) -> None:
        rl = RateLimiter()
        for _ in range(3):
            rl.check("1.1.1.1", limit=3)
        allowed, retry = rl.check("1.1.1.1", limit=3)
        assert allowed is False
        assert retry >= 1

    def test_different_ips_independent(self) -> None:
        rl = RateLimiter()
        rl.check("1.1.1.1", limit=1)
        allowed, _ = rl.check("2.2.2.2", limit=1)
        assert allowed is True

    def test_limit_zero_always_allows(self) -> None:
        rl = RateLimiter()
        allowed, _ = rl.check("1.1.1.1", limit=0)
        assert allowed is True

    def test_window_expires(self) -> None:
        rl = RateLimiter()
        rl.check("1.1.1.1", limit=1, window_seconds=0)
        time.sleep(0.01)
        allowed, _ = rl.check("1.1.1.1", limit=1, window_seconds=0)
        # With zero window, nothing should persist
        assert allowed is True


def _make_request(settings: Settings, client_ip: str = "10.0.0.99") -> object:
    """Build a minimal Starlette Request for testing."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/test",
        "raw_path": b"/test",
        "query_string": b"",
        "headers": [],
        "client": (client_ip, 12345),
        "server": ("testserver", 80),
        "app": MagicMock(),
    }
    req = Request(scope)
    req.app.state.settings = settings  # type: ignore[union-attr]
    return req


class TestEnforceLimit:
    def test_disabled_is_noop(self) -> None:
        settings = Settings(rate_limit_enabled=False)
        request = _make_request(settings)
        # Should not raise
        enforce_limit(request, limit=1)  # type: ignore[arg-type]

    def test_enabled_raises_429_on_exceed(self) -> None:
        from fastapi import HTTPException

        settings = Settings(rate_limit_enabled=True)
        # Use a unique IP per test to avoid sharing the module-level limiter state
        request = _make_request(settings, client_ip="10.99.88.77")

        # First call allowed
        enforce_limit(request, limit=1)  # type: ignore[arg-type]
        # Second call raises
        with pytest.raises(HTTPException) as exc_info:
            enforce_limit(request, limit=1)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 429  # noqa: PLR2004
        assert exc_info.value.headers is not None
        assert "Retry-After" in exc_info.value.headers
