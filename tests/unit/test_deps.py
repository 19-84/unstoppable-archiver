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
    require_metrics_token,
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

    async def test_partial_match_prefix_still_rejected(self) -> None:
        """A key that matches the first N bytes of the real one but
        differs later must be rejected — pins that we don't short-
        circuit on prefix match. Plain `==` on strings would already
        reject this, but the wider point is that the comparator is
        hmac.compare_digest which is constant-time regardless of
        match position, so a timing-attack adversary can't recover
        the key byte-by-byte."""
        # Both 9 chars; first 6 match, differ at index 6.
        request = _make_request(
            headers={"authorization": "Bearer secret321"},
            api_key="secret123",
        )
        with pytest.raises(HTTPException) as exc:
            await require_api_key(request)
        assert exc.value.status_code == 401  # noqa: PLR2004

    async def test_empty_x_api_key_header_rejected(self) -> None:
        """An explicit empty X-API-Key header must be a 401, not an
        auth bypass. Previously a request with ``X-API-Key:`` would
        run ``"" == key`` and be rejected only because the secret was
        non-empty — we now also short-circuit on empty header value
        BEFORE the constant-time compare to keep the intent obvious."""
        request = _make_request(
            headers={"x-api-key": ""},
            api_key="secret123",
        )
        with pytest.raises(HTTPException) as exc:
            await require_api_key(request)
        assert exc.value.status_code == 401  # noqa: PLR2004


def _make_metrics_request(
    headers: dict[str, str] | None = None,
    metrics_token: str = "",
) -> MagicMock:
    """Create a mock Request with metrics_token settings and headers."""
    from pydantic import SecretStr

    request = MagicMock()
    request.app.state.settings.metrics_token = SecretStr(metrics_token)
    request.headers = headers or {}
    return request


class TestRequireMetricsToken:
    async def test_no_token_configured_allows_all(self) -> None:
        request = _make_metrics_request(metrics_token="")
        await require_metrics_token(request)  # Should not raise

    async def test_bearer_token_valid(self) -> None:
        request = _make_metrics_request(
            headers={"authorization": "Bearer scrape-secret"},
            metrics_token="scrape-secret",  # noqa: S106
        )
        await require_metrics_token(request)

    async def test_missing_token_raises_401(self) -> None:
        request = _make_metrics_request(metrics_token="scrape-secret")  # noqa: S106
        with pytest.raises(HTTPException) as exc:
            await require_metrics_token(request)
        assert exc.value.status_code == 401  # noqa: PLR2004

    async def test_wrong_token_raises_401(self) -> None:
        request = _make_metrics_request(
            headers={"authorization": "Bearer wrong"},
            metrics_token="scrape-secret",  # noqa: S106
        )
        with pytest.raises(HTTPException) as exc:
            await require_metrics_token(request)
        assert exc.value.status_code == 401  # noqa: PLR2004

    async def test_x_api_key_header_not_accepted(self) -> None:
        """Only Bearer is accepted. Prometheus' scrape `authorization`
        sends Bearer, and /metrics has no human callers — so the
        X-API-Key path require_api_key allows is deliberately not
        mirrored here. An X-API-Key-only request must 401."""
        request = _make_metrics_request(
            headers={"x-api-key": "scrape-secret"},
            metrics_token="scrape-secret",  # noqa: S106
        )
        with pytest.raises(HTTPException) as exc:
            await require_metrics_token(request)
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
