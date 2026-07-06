# ABOUTME: Shared outbound HTTP layer: pooled client, retries with backoff,
# ABOUTME: Retry-After honoring, body-size caps, and per-hop SSRF validation
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false
"""Outbound HTTP fetch helper used by the capture/fallback pipeline.

Every direct (non-browser) request to an upstream service should go
through :func:`fetch` instead of constructing an ad-hoc
``httpx.AsyncClient``. It provides, in one place:

- **Connection reuse** — one pooled client per event loop instead of a
  fresh TCP+TLS handshake per request.
- **Retries with backoff** — transport errors and retryable statuses
  (429/502/503/504) are retried with exponential backoff + jitter,
  honoring ``Retry-After`` when the upstream sends one. Non-retryable
  statuses (404, 403, ...) are returned to the caller unchanged.
- **Body caps** — responses are streamed and aborted once they exceed
  ``max_bytes``, so a huge or hostile body can't OOM the worker.
- **SSRF guard** — with ``guard_private_ips=True`` the target URL and
  every redirect hop are re-validated against the private/internal IP
  rules in :mod:`archiver.url_safety`.
- **UA hygiene** — the rotating real-browser User-Agent pool is applied
  by default so the httpx library UA never leaks upstream.
"""

from __future__ import annotations

import asyncio
import json as _json
import random
import re
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import structlog
from beartype import beartype
from icontract import require

from archiver import user_agents
from archiver.errors import BodyTooLargeError, UnsafeURLError, UpstreamError
from archiver.url_safety import check_url_safety_async

log = structlog.get_logger()

# Statuses worth retrying: rate limits and transient upstream failures.
RETRY_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})

# Backoff: 2s, 4s, 8s, ... capped at 120s (plus 0-25% jitter, applied
# before the cap). Retry-After from the upstream overrides the computed
# delay when larger, still subject to the cap.
_BACKOFF_BASE_S = 2.0
_BACKOFF_MAX_S = 120.0

# 32 MiB default body budget — generous for HTML/JSON API responses,
# small enough that a runaway body can't take down a worker with
# max_concurrent_captures in the low single digits.
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

_MAX_REDIRECTS = 10
_STREAM_CHUNK_TARGET = 65_536

_DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0, read=30.0, write=10.0, pool=10.0
)
_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

# Indirection so unit tests can patch delays out without touching the
# global asyncio.sleep.
_sleep = asyncio.sleep

# One pooled client per event loop. Keyed weakly so a torn-down loop
# (each pytest-asyncio test gets its own) drops its client instead of
# accumulating forever.
_shared_clients: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, httpx.AsyncClient
] = weakref.WeakKeyDictionary()


def _shared_client() -> httpx.AsyncClient:
    """Return the pooled client for the running loop, creating if needed."""
    loop = asyncio.get_running_loop()
    client = _shared_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=_DEFAULT_TIMEOUT,
            limits=_LIMITS,
        )
        _shared_clients[loop] = client
    return client


async def aclose_shared_client() -> None:
    """Close the running loop's pooled client (worker/app shutdown)."""
    loop = asyncio.get_running_loop()
    client = _shared_clients.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()


@dataclass(frozen=True)
class FetchResponse:
    """A fully-read HTTP response with the body already size-capped."""

    status_code: int
    headers: httpx.Headers
    content: bytes
    url: str  # final URL after any followed redirects

    @property
    def text(self) -> str:
        """Body decoded via the content-type charset (utf-8 fallback)."""
        content_type = self.headers.get("content-type", "")
        match = re.search(r"charset=([\w.-]+)", content_type)
        charset = match.group(1) if match else "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse the body as JSON."""
        return _json.loads(self.content)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header: delta-seconds or an HTTP-date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max(0.0, (dt - datetime.now(UTC)).total_seconds())


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    """Delay before retry `attempt` (1-based), with jitter, capped."""
    backoff = _BACKOFF_BASE_S * (2 ** (attempt - 1))
    delay = max(backoff, retry_after or 0.0)
    # Jitter desynchronizes concurrent captures retrying against the
    # same upstream so they don't re-hammer it in lockstep.
    delay *= 1.0 + random.uniform(0.0, 0.25)  # noqa: S311
    return min(delay, _BACKOFF_MAX_S)


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """Stream the body, aborting once it exceeds `max_bytes`."""
    declared = resp.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise BodyTooLargeError(
                    f"Content-Length {declared} exceeds cap "
                    f"{max_bytes}: {resp.request.url}"
                )
        except ValueError:
            pass  # Malformed header — fall through to streamed check.
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes(_STREAM_CHUNK_TARGET):
        total += len(chunk)
        if total > max_bytes:
            raise BodyTooLargeError(
                f"Body exceeded cap {max_bytes} bytes: {resp.request.url}"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@beartype
@require(lambda attempts: attempts >= 1, "attempts must be >= 1")
@require(lambda max_bytes: max_bytes > 0, "max_bytes must be positive")
async def fetch(  # noqa: PLR0913
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    timeout: float | None = None,
    proxy: str | None = None,
    follow_redirects: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    attempts: int = 3,
    guard_private_ips: bool = False,
) -> FetchResponse:
    """Fetch `url` with retries, backoff, body caps, and SSRF guarding.

    Returns a :class:`FetchResponse` for any terminal status code —
    callers keep their existing ``status_code`` branching. Raises:

    - :class:`UpstreamError` — transport failure on every attempt, or
      too many redirects.
    - :class:`BodyTooLargeError` — body exceeded ``max_bytes``.
    - :class:`UnsafeURLError` — ``guard_private_ips`` rejected the URL
      or one of its redirect hops.

    Retryable statuses (429/502/503/504) are retried with backoff and
    ``Retry-After``; on the final attempt the response is returned so
    the caller can distinguish "rate-limited" from "not found".

    ``proxy`` requests use a per-call client (proxies rotate too often
    to pool); direct requests share a pooled per-loop client. Redirects
    are always followed with GET, mirroring browser behavior.
    """
    target = str(httpx.URL(url, params=params)) if params else url

    if proxy is not None:
        async with httpx.AsyncClient(
            proxy=proxy,
            follow_redirects=False,
            timeout=_DEFAULT_TIMEOUT,
            limits=_LIMITS,
        ) as client:
            return await _fetch_with_client(
                client, method, target,
                headers=headers, timeout=timeout,
                follow_redirects=follow_redirects, max_bytes=max_bytes,
                attempts=attempts, guard_private_ips=guard_private_ips,
            )
    return await _fetch_with_client(
        _shared_client(), method, target,
        headers=headers, timeout=timeout,
        follow_redirects=follow_redirects, max_bytes=max_bytes,
        attempts=attempts, guard_private_ips=guard_private_ips,
    )


async def _fetch_with_client(  # noqa: PLR0913
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None,
    timeout: float | None,
    follow_redirects: bool,
    max_bytes: int,
    attempts: int,
    guard_private_ips: bool,
) -> FetchResponse:
    """Retry/redirect loop shared by the pooled and per-proxy paths."""
    request_headers = {"User-Agent": user_agents.pick(), **(headers or {})}
    request_timeout: float | httpx.Timeout = (
        timeout if timeout is not None else _DEFAULT_TIMEOUT
    )

    current_url = url
    current_method = method
    attempt = 1
    redirects = 0
    while True:
        if guard_private_ips:
            error = await check_url_safety_async(current_url)
            if error:
                raise UnsafeURLError(
                    f"Refusing to fetch {current_url}: {error}"
                )

        delay: float | None = None
        try:
            async with client.stream(
                current_method,
                current_url,
                headers=request_headers,
                timeout=request_timeout,
            ) as resp:
                is_last = attempt >= attempts
                if resp.status_code in RETRY_STATUSES and not is_last:
                    retry_after = _parse_retry_after(
                        resp.headers.get("retry-after")
                    )
                    delay = _retry_delay(attempt, retry_after)
                elif resp.has_redirect_location and follow_redirects:
                    redirects += 1
                    if redirects > _MAX_REDIRECTS:
                        raise UpstreamError(
                            f"Too many redirects fetching {url}"
                        )
                    current_url = str(
                        httpx.URL(current_url).join(
                            resp.headers["location"]
                        )
                    )
                    current_method = "GET"
                    continue
                else:
                    content = await _read_capped(resp, max_bytes)
                    return FetchResponse(
                        status_code=resp.status_code,
                        headers=resp.headers,
                        content=content,
                        url=current_url,
                    )
        except httpx.TransportError as exc:
            if attempt >= attempts:
                raise UpstreamError(
                    f"{type(exc).__name__} fetching {current_url}: {exc}"
                ) from exc
            delay = _retry_delay(attempt, None)

        # Retryable status or transport error — back off and go again.
        attempt += 1
        log.debug(
            "http.retrying",
            url=current_url,
            attempt=attempt,
            delay=round(delay or 0.0, 2),
        )
        if delay:
            await _sleep(delay)
