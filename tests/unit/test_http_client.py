# ABOUTME: Unit tests for the shared outbound HTTP layer (archiver.http_client)
# ABOUTME: Covers retries, backoff, Retry-After, body caps, redirects, SSRF guard
"""Tests for archiver.http_client."""

from __future__ import annotations

import httpx
import pytest
import respx

from archiver import http_client
from archiver.errors import BodyTooLargeError, UnsafeURLError, UpstreamError
from archiver.http_client import (
    FetchResponse,
    _parse_retry_after,
    _retry_delay,
    aclose_shared_client,
    fetch,
)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Capture retry delays instead of actually sleeping."""
    recorded: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(http_client, "_sleep", _fake_sleep)
    return recorded


class TestFetchBasics:
    @respx.mock
    async def test_returns_response(self) -> None:
        respx.get("https://upstream.example/api").respond(
            200, content=b'{"ok": true}',
            headers={"content-type": "application/json"},
        )
        resp = await fetch("https://upstream.example/api")
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.json() == {"ok": True}
        assert resp.url == "https://upstream.example/api"

    @respx.mock
    async def test_params_merged_into_url(self) -> None:
        route = respx.get(
            "https://upstream.example/api", params={"q": "x"}
        ).respond(200, content=b"hit")
        resp = await fetch(
            "https://upstream.example/api", params={"q": "x"}
        )
        assert resp.content == b"hit"
        assert route.called

    @respx.mock
    async def test_default_ua_is_from_rotating_pool(self) -> None:
        route = respx.get("https://upstream.example/").respond(200)
        await fetch("https://upstream.example/")
        sent_ua = route.calls.last.request.headers["user-agent"]
        assert "httpx" not in sent_ua.lower()
        assert "Mozilla" in sent_ua

    @respx.mock
    async def test_caller_headers_override_ua(self) -> None:
        route = respx.get("https://upstream.example/").respond(200)
        await fetch(
            "https://upstream.example/",
            headers={"User-Agent": "custom-agent"},
        )
        assert route.calls.last.request.headers["user-agent"] == (
            "custom-agent"
        )

    @respx.mock
    async def test_non_retryable_status_returned_without_retry(
        self, sleeps: list[float]
    ) -> None:
        route = respx.get("https://upstream.example/missing").respond(404)
        resp = await fetch("https://upstream.example/missing")
        assert resp.status_code == 404  # noqa: PLR2004
        assert route.call_count == 1
        assert sleeps == []

    @respx.mock
    async def test_shared_client_is_reused_across_fetches(self) -> None:
        respx.get("https://upstream.example/").respond(200)
        await fetch("https://upstream.example/")
        first = http_client._shared_client()
        await fetch("https://upstream.example/")
        assert http_client._shared_client() is first
        await aclose_shared_client()
        assert first.is_closed


class TestRetries:
    @respx.mock
    async def test_retries_503_then_succeeds(
        self, sleeps: list[float]
    ) -> None:
        route = respx.get("https://upstream.example/")
        route.side_effect = [
            httpx.Response(503),
            httpx.Response(200, content=b"recovered"),
        ]
        resp = await fetch("https://upstream.example/", attempts=3)
        assert resp.status_code == 200  # noqa: PLR2004
        assert resp.content == b"recovered"
        assert route.call_count == 2  # noqa: PLR2004
        assert len(sleeps) == 1
        assert sleeps[0] >= 2.0  # base backoff  # noqa: PLR2004

    @respx.mock
    async def test_retry_after_seconds_honored(
        self, sleeps: list[float]
    ) -> None:
        route = respx.get("https://upstream.example/")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "9"}),
            httpx.Response(200),
        ]
        resp = await fetch("https://upstream.example/", attempts=2)
        assert resp.status_code == 200  # noqa: PLR2004
        assert sleeps[0] >= 9.0  # noqa: PLR2004

    @respx.mock
    async def test_retryable_status_returned_on_final_attempt(
        self, sleeps: list[float]
    ) -> None:
        route = respx.get("https://upstream.example/").respond(429)
        resp = await fetch("https://upstream.example/", attempts=1)
        assert resp.status_code == 429  # noqa: PLR2004
        assert route.call_count == 1
        assert sleeps == []

    @respx.mock
    async def test_transport_error_retried_then_succeeds(
        self, sleeps: list[float]
    ) -> None:
        route = respx.get("https://upstream.example/")
        route.side_effect = [
            httpx.ConnectError("refused"),
            httpx.Response(200, content=b"up"),
        ]
        resp = await fetch("https://upstream.example/", attempts=2)
        assert resp.content == b"up"
        assert len(sleeps) == 1

    @respx.mock
    async def test_transport_error_exhausts_to_upstream_error(
        self, sleeps: list[float]
    ) -> None:
        route = respx.get("https://upstream.example/")
        route.side_effect = httpx.ConnectError("refused")
        with pytest.raises(UpstreamError):
            await fetch("https://upstream.example/", attempts=3)
        assert route.call_count == 3  # noqa: PLR2004
        assert len(sleeps) == 2  # noqa: PLR2004

    def test_retry_delay_grows_and_caps(self) -> None:
        d1 = _retry_delay(1, None)
        d5 = _retry_delay(5, None)
        assert 2.0 <= d1 <= 2.5  # noqa: PLR2004
        assert d5 >= d1
        assert _retry_delay(50, None) <= 120.0  # noqa: PLR2004

    def test_retry_delay_prefers_larger_retry_after(self) -> None:
        assert _retry_delay(1, 30.0) >= 30.0  # noqa: PLR2004

    def test_parse_retry_after_forms(self) -> None:
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("7") == 7.0  # noqa: PLR2004
        assert _parse_retry_after("-5") == 0.0
        assert _parse_retry_after("garbage") is None
        # HTTP-date in the past clamps to 0
        parsed = _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT")
        assert parsed == 0.0


class TestBodyCaps:
    @respx.mock
    async def test_content_length_over_cap_raises(self) -> None:
        respx.get("https://upstream.example/big").respond(
            200, content=b"x" * 2048
        )
        with pytest.raises(BodyTooLargeError):
            await fetch("https://upstream.example/big", max_bytes=1024)

    @respx.mock
    async def test_streamed_body_over_cap_raises(self) -> None:
        # No Content-Length: stream chunks so the cap triggers mid-read.
        respx.get("https://upstream.example/chunked").mock(
            return_value=httpx.Response(200, stream=_Stream())
        )
        with pytest.raises(BodyTooLargeError):
            await fetch(
                "https://upstream.example/chunked", max_bytes=1024
            )

    @respx.mock
    async def test_body_under_cap_ok(self) -> None:
        respx.get("https://upstream.example/small").respond(
            200, content=b"z" * 100
        )
        resp = await fetch(
            "https://upstream.example/small", max_bytes=1024
        )
        assert len(resp.content) == 100  # noqa: PLR2004


class _Stream(httpx.AsyncByteStream):
    """Chunked body with no Content-Length header."""

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for _ in range(4):
            yield b"y" * 512


class TestRedirects:
    @respx.mock
    async def test_redirect_followed_when_enabled(self) -> None:
        respx.get("https://upstream.example/old").respond(
            302, headers={"location": "https://upstream.example/new"}
        )
        respx.get("https://upstream.example/new").respond(
            200, content=b"moved"
        )
        resp = await fetch(
            "https://upstream.example/old", follow_redirects=True
        )
        assert resp.content == b"moved"
        assert resp.url == "https://upstream.example/new"

    @respx.mock
    async def test_redirect_returned_when_disabled(self) -> None:
        respx.get("https://upstream.example/old").respond(
            302, headers={"location": "https://upstream.example/new"}
        )
        resp = await fetch("https://upstream.example/old")
        assert resp.status_code == 302  # noqa: PLR2004

    @respx.mock
    async def test_relative_redirect_resolved(self) -> None:
        respx.get("https://upstream.example/a/old").respond(
            301, headers={"location": "../new"}
        )
        respx.get("https://upstream.example/new").respond(200)
        resp = await fetch(
            "https://upstream.example/a/old", follow_redirects=True
        )
        assert resp.url == "https://upstream.example/new"

    @respx.mock
    async def test_redirect_loop_raises(self) -> None:
        respx.get("https://upstream.example/loop").respond(
            302, headers={"location": "https://upstream.example/loop"}
        )
        with pytest.raises(UpstreamError, match="redirects"):
            await fetch(
                "https://upstream.example/loop", follow_redirects=True
            )


class TestSSRFGuard:
    @respx.mock
    async def test_private_ip_target_blocked(self) -> None:
        route = respx.get("http://127.0.0.1/").respond(200)
        with pytest.raises(UnsafeURLError):
            await fetch("http://127.0.0.1/", guard_private_ips=True)
        assert not route.called

    @respx.mock
    async def test_redirect_to_private_ip_blocked(self) -> None:
        respx.get("https://upstream.example/sneaky").respond(
            302, headers={"location": "http://169.254.169.254/latest/"}
        )
        inner = respx.get("http://169.254.169.254/latest/").respond(200)
        with pytest.raises(UnsafeURLError):
            await fetch(
                "https://upstream.example/sneaky",
                follow_redirects=True,
                guard_private_ips=True,
            )
        assert not inner.called

    @respx.mock
    async def test_guard_off_by_default(self) -> None:
        respx.get("http://127.0.0.1/").respond(200, content=b"local")
        resp = await fetch("http://127.0.0.1/")
        assert resp.content == b"local"


class TestFetchResponse:
    def test_text_uses_charset_from_content_type(self) -> None:
        resp = FetchResponse(
            status_code=200,
            headers=httpx.Headers(
                {"content-type": "text/html; charset=latin-1"}
            ),
            content="café".encode("latin-1"),
            url="https://x.example/",
        )
        assert resp.text == "café"

    def test_text_falls_back_on_unknown_charset(self) -> None:
        resp = FetchResponse(
            status_code=200,
            headers=httpx.Headers(
                {"content-type": "text/html; charset=not-a-charset"}
            ),
            content=b"plain",
            url="https://x.example/",
        )
        assert resp.text == "plain"

    def test_text_defaults_to_utf8(self) -> None:
        resp = FetchResponse(
            status_code=200,
            headers=httpx.Headers({}),
            content="héllo".encode(),
            url="https://x.example/",
        )
        assert resp.text == "héllo"
