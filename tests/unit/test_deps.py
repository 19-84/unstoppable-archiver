# ABOUTME: Unit tests for FastAPI dependency injection functions
# ABOUTME: Tests API key auth checking with Bearer and X-API-Key headers
"""Tests for dependency injection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from archiver.deps import require_api_key


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
