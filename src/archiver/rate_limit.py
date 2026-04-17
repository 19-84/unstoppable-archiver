# ABOUTME: Simple in-memory per-IP rate limiter with sliding window
# ABOUTME: Gated by settings.rate_limit_enabled; returns 429 with Retry-After
"""Per-IP rate limiting with in-memory sliding window."""

from __future__ import annotations

import time
from collections import defaultdict, deque

import structlog
from beartype import beartype
from fastapi import HTTPException, Request, status

from archiver.config import Settings

log = structlog.get_logger()


class RateLimiter:
    """In-memory sliding-window rate limiter keyed by IP.

    For single-worker deployments. Multi-worker needs Redis.
    """

    def __init__(self) -> None:
        # ip -> deque of timestamps
        self._windows: dict[str, deque[float]] = defaultdict(deque)

    @beartype
    def check(
        self, ip: str, limit: int, window_seconds: int = 3600
    ) -> tuple[bool, int]:
        """Check if IP is within limit.

        Returns (allowed, retry_after_seconds).
        """
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        window = self._windows[ip]
        cutoff = now - window_seconds
        # Trim old entries
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= limit:
            retry_after = int(window[0] + window_seconds - now) + 1
            return False, max(retry_after, 1)

        window.append(now)
        return True, 0


_global_limiter = RateLimiter()


@beartype
def enforce_limit(
    request: Request, limit: int, window_seconds: int = 3600
) -> None:
    """Enforce rate limit for the current request. Raises 429 on exceed."""
    settings: Settings = request.app.state.settings
    if not settings.rate_limit_enabled:
        return

    from archiver.deps import get_client_ip_hash

    ip = get_client_ip_hash(request) or "unknown"
    allowed, retry_after = _global_limiter.check(ip, limit, window_seconds)
    if not allowed:
        from archiver.metrics import rate_limit_exceeded_total

        log.warning("rate_limit.exceeded", ip=ip, limit=limit)
        rate_limit_exceeded_total.labels(endpoint=request.url.path).inc()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({limit}/hour). Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
