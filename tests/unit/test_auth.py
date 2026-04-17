# ABOUTME: Tests for admin auth — bcrypt password hashing and verification
# ABOUTME: Covers hash round-trip, wrong password rejection, empty/malformed input
"""Tests for auth module."""

from __future__ import annotations

from archiver.auth import hash_password, verify_password


class TestHashPassword:
    def test_round_trip(self) -> None:
        hashed = hash_password("correct-horse-battery-staple")
        assert verify_password("correct-horse-battery-staple", hashed) is True

    def test_different_hash_each_time(self) -> None:
        # bcrypt uses random salt → same password → different hash
        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2

    def test_hash_is_bcrypt_format(self) -> None:
        hashed = hash_password("x")
        assert hashed.startswith("$2b$")


class TestVerifyPassword:
    def test_wrong_password_fails(self) -> None:
        hashed = hash_password("correct")
        assert verify_password("wrong", hashed) is False

    def test_empty_plain_fails(self) -> None:
        hashed = hash_password("anything")
        assert verify_password("", hashed) is False

    def test_empty_hash_fails(self) -> None:
        assert verify_password("anything", "") is False

    def test_malformed_hash_fails(self) -> None:
        assert verify_password("anything", "not-a-bcrypt-hash") is False


def _make_request_with_session(settings: object, session: dict) -> object:  # type: ignore[type-arg]
    """Minimal Request with a session dict."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/admin/",
        "raw_path": b"/admin/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "session": session,
    }
    req = Request(scope)
    return req


class TestRequireAdmin:
    async def test_raises_404_when_admin_disabled(self) -> None:
        from fastapi import HTTPException

        from archiver.auth import require_admin
        from archiver.config import Settings

        settings = Settings()  # no admin_password_hash
        request = _make_request_with_session(settings, {})

        import pytest
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(request, settings)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 404  # noqa: PLR2004

    async def test_raises_401_when_not_logged_in(self) -> None:
        from fastapi import HTTPException

        from archiver.auth import require_admin
        from archiver.config import Settings

        settings = Settings(
            admin_password_hash=hash_password("x"),  # type: ignore[arg-type]
        )
        request = _make_request_with_session(settings, {})

        import pytest
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(request, settings)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 401  # noqa: PLR2004

    async def test_returns_admin_when_session_set(self) -> None:
        from archiver.auth import require_admin
        from archiver.config import Settings

        settings = Settings(
            admin_password_hash=hash_password("x"),  # type: ignore[arg-type]
        )
        request = _make_request_with_session(settings, {"admin": True})

        result = await require_admin(request, settings)  # type: ignore[arg-type]
        assert result == "admin"
