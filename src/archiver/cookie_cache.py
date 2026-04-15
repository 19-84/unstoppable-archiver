# ABOUTME: In-memory TTL cache for Cloudflare cf_clearance cookies
# ABOUTME: Caches solved challenge cookies per domain to avoid re-solving (valid ~15 days)
"""Cloudflare cf_clearance cookie cache."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import structlog
from beartype import beartype

log = structlog.get_logger()

_DEFAULT_TTL = 15 * 24 * 3600  # 15 days in seconds


@dataclass(frozen=True)
class CachedCookie:
    """A cached cookie with domain, value, and expiry."""

    name: str
    value: str
    domain: str
    path: str
    expires: datetime


@dataclass
class CfClearanceCache:
    """In-memory TTL cache for cf_clearance cookies, keyed by domain."""

    ttl_seconds: int = _DEFAULT_TTL
    _cache: dict[str, CachedCookie] = field(default_factory=dict)

    @beartype
    def get(self, domain: str) -> CachedCookie | None:
        """Return cached cookie for domain, or None if expired/missing."""
        domain = _normalize_domain(domain)
        cookie = self._cache.get(domain)
        if cookie and cookie.expires > datetime.now(UTC):
            return cookie
        if cookie:
            del self._cache[domain]
        return None

    @beartype
    def put(self, domain: str, name: str, value: str, path: str = "/") -> None:
        """Cache a cookie for domain with TTL."""
        domain = _normalize_domain(domain)
        cookie = CachedCookie(
            name=name,
            value=value,
            domain=domain,
            path=path,
            expires=datetime.now(UTC) + timedelta(seconds=self.ttl_seconds),
        )
        self._cache[domain] = cookie
        log.info("cookie_cache.stored", domain=domain, name=name)

    @beartype
    def get_for_url(self, url: str) -> CachedCookie | None:
        """Lookup cached cookie by URL."""
        hostname = urlparse(url).hostname or ""
        return self.get(hostname)


@beartype
def _normalize_domain(domain: str) -> str:
    """Strip www. prefix and leading dot for consistent domain keying."""
    if domain.startswith("."):
        domain = domain[1:]
    if domain.startswith("www."):
        domain = domain[4:]
    return domain
