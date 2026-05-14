# ABOUTME: Domain blocklist with allowlist overrides — longest-match wins
# ABOUTME: Loads apex domains from files/URLs/inline; O(1) set lookup + subdomain walk
"""Domain blocklist with allowlist overrides."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog
from beartype import beartype

from archiver.config import Settings

log = structlog.get_logger()


@dataclass
class DomainBlocklist:
    """Domain blocklist with allowlist overrides.

    Apex domains match all subdomains. Allowlist entries override
    blocklist entries using longest-match-wins semantics.
    """

    blocked: set[str] = field(default_factory=set[str])
    allowed: set[str] = field(default_factory=set[str])
    last_loaded: datetime | None = None

    @beartype
    def check(self, hostname: str) -> str | None:
        """Return block reason, or None if allowed/not-blocked.

        Walks up the subdomain chain (most specific first); first hit
        in either list wins.
        """
        if not hostname:
            return None
        hostname = hostname.lower().strip().rstrip(".")
        if hostname.startswith("www."):
            hostname = hostname[4:]

        for candidate in _walk_up(hostname):
            if candidate in self.allowed:
                return None
            if candidate in self.blocked:
                return f"Domain blocked: {candidate}"
        return None

    @property
    def blocked_count(self) -> int:
        return len(self.blocked)

    @property
    def allowed_count(self) -> int:
        return len(self.allowed)


@beartype
def _walk_up(hostname: str) -> list[str]:
    """Return hostname variants from most specific to least specific."""
    parts = hostname.split(".")
    # Skip the TLD alone ("com") — too broad to match
    return [".".join(parts[i:]) for i in range(len(parts) - 1)]


@beartype
def _parse_domain_list(content: str) -> set[str]:
    """Parse a hosts file or plain domain list into a set of apex domains.

    Accepts:
      - 0.0.0.0 example.com  (hosts file)
      - 127.0.0.1 example.com
      - example.com           (plain list)
      - # comments            (ignored)
    """
    domains: set[str] = set()
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        # Hosts file: skip the IP (first token), take the domain
        candidate = tokens[-1].lower().strip().rstrip(".")
        if candidate.startswith("www."):
            candidate = candidate[4:]
        # Sanity: must contain a dot, no spaces/slashes
        if "." in candidate and "/" not in candidate and " " not in candidate:
            domains.add(candidate)
    return domains


@beartype
async def _fetch_url(url: str, timeout: float = 30.0) -> str:
    """Fetch a remote blocklist URL."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


@beartype
async def _load_sources(
    file_path: Path | None,
    urls: str,
    inline: str,
) -> set[str]:
    """Union of file + URL(s) + inline comma-separated domains."""
    domains: set[str] = set()

    if file_path and file_path.exists():
        try:
            domains |= _parse_domain_list(file_path.read_text(encoding="utf-8"))
            log.info("blocklist.file_loaded", path=str(file_path))
        except OSError as exc:
            log.warning("blocklist.file_error", path=str(file_path), error=str(exc))

    for url in [u.strip() for u in urls.split(",") if u.strip()]:
        try:
            domains |= _parse_domain_list(await _fetch_url(url))
            log.info("blocklist.url_loaded", url=url)
        except Exception as exc:
            log.warning("blocklist.url_error", url=url, error=str(exc))

    if inline:
        domains |= _parse_domain_list(inline.replace(",", "\n"))

    return domains


@beartype
async def load_blocklist(settings: Settings) -> DomainBlocklist:
    """Load blocklist + allowlist from all configured sources."""
    blocked = await _load_sources(
        settings.blocklist_file,
        settings.blocklist_urls,
        settings.blocklist_domains,
    )
    allowed = await _load_sources(
        settings.allowlist_file,
        settings.allowlist_urls,
        settings.allowlist_domains,
    )
    log.info(
        "blocklist.loaded",
        blocked=len(blocked),
        allowed=len(allowed),
    )
    return DomainBlocklist(
        blocked=blocked,
        allowed=allowed,
        last_loaded=datetime.now(UTC),
    )
