# ABOUTME: Unit tests for FastAPI dependency injection functions
# ABOUTME: Tests API key auth checking with Bearer and X-API-Key headers
"""Tests for dependency injection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from archiver.deps import (
    get_blocklist,
    get_client_ip_hash,
    get_db,
    get_settings,
    require_api_key,
)


def _make_request(
    headers: dict[str, str] | None = None,
    api_key: str = "",
) -> MagicMock:
    """Create a mock Request with settings and headers."""
    from pydantic import SecretStr

    request = MagicMock()
    request.app.state.settings.api_key = SecretStr(api_key)
    request.headers = headers or {}
    return request


class TestRequireApiKey:
    async def test_no_key_configured_allows_all(self) -> None:
        request = _make_request(api_key="")
        await require_api_key(request)  # Should not raise

    async def test_bearer_token_valid(self) -> None:
        request = _make_request(
            headers={"authorization": "Bearer secret123"},
            api_key="secret123",
        )
        await require_api_key(request)

    async def test_x_api_key_valid(self) -> None:
        request = _make_request(
            headers={"x-api-key": "secret123"},
            api_key="secret123",
        )
        await require_api_key(request)

    async def test_missing_key_raises_401(self) -> None:
        request = _make_request(api_key="secret123")
        with pytest.raises(HTTPException) as exc:
            await require_api_key(request)
        assert exc.value.status_code == 401  # noqa: PLR2004

    async def test_wrong_key_raises_401(self) -> None:
        request = _make_request(
            headers={"authorization": "Bearer wrong"},
            api_key="secret123",
        )
        with pytest.raises(HTTPException) as exc:
            await require_api_key(request)
        assert exc.value.status_code == 401  # noqa: PLR2004


class TestClientIpHash:
    def _request(
        self,
        host: str | None,
        trusted_proxies: bool = False,
        xff: str | None = None,
    ) -> MagicMock:
        from pydantic import SecretStr

        request = MagicMock()
        request.app.state.settings.trusted_proxies = trusted_proxies
        request.app.state.settings.ip_hash_salt = SecretStr("peppersalt")
        request.app.state.settings.session_secret = SecretStr("sessionx")
        request.client = MagicMock(host=host) if host is not None else None
        request.headers = {"x-forwarded-for": xff} if xff else {}
        return request

    def test_xff_used_when_proxies_trusted(self) -> None:
        a = get_client_ip_hash(
            self._request(host="10.0.0.1", trusted_proxies=True, xff="1.2.3.4")
        )
        b = get_client_ip_hash(
            self._request(host="10.0.0.1", trusted_proxies=True, xff="1.2.3.4")
        )
        # Same XFF → same hash; prefix-only (not raw IP).
        assert a == b
        assert "1.2.3.4" not in a

    def test_xff_ignored_when_proxies_untrusted(self) -> None:
        a = get_client_ip_hash(
            self._request(host="10.0.0.1", trusted_proxies=False, xff="1.2.3.4")
        )
        b = get_client_ip_hash(
            self._request(host="10.0.0.2", trusted_proxies=False, xff="1.2.3.4")
        )
        assert a != b

    def test_missing_client_returns_empty(self) -> None:
        assert get_client_ip_hash(self._request(host=None)) == ""

    def test_empty_xff_falls_back_to_client_host(self) -> None:
        """Trusted proxies + empty XFF header → use client.host."""
        a = get_client_ip_hash(
            self._request(host="10.0.0.1", trusted_proxies=True, xff=None)
        )
        assert a != ""


class TestSimpleAccessors:
    async def test_get_db_yields_acquired_connection(self) -> None:
        from unittest.mock import AsyncMock

        mock_conn = AsyncMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire.return_value = mock_ctx
        request = MagicMock()
        request.app.state.pool = pool

        async for conn in get_db(request):
            assert conn is mock_conn

    def test_get_settings_returns_app_state(self) -> None:
        request = MagicMock()
        sentinel = object()
        request.app.state.settings = sentinel
        assert get_settings(request) is sentinel

    def test_get_blocklist_returns_app_state(self) -> None:
        request = MagicMock()
        sentinel = object()
        request.app.state.blocklist = sentinel
        assert get_blocklist(request) is sentinel
