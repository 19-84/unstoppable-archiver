# ABOUTME: URL safety validation to prevent SSRF via the capture pipeline
# ABOUTME: Blocks private IPs, loopback, link-local, and Docker-internal hostnames
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false
"""URL safety checks to prevent Server-Side Request Forgery."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Sequence
from urllib.parse import urlparse

from beartype import beartype
from icontract import require

from archiver.blocklist import DomainBlocklist

# Hostnames that resolve to internal Docker services
BLOCKED_HOSTNAMES: frozenset[str] = frozenset({
    "localhost",
    "postgres",
    "tor",
    "i2p",
    "web",
    "app",
    "worker",
    "redis",
    "0.0.0.0",  # noqa: S104
})


@beartype
@require(lambda url: len(url) > 0, "URL must not be empty")
def check_url_safety(
    url: str, blocklist: DomainBlocklist | None = None
) -> str | None:
    """Check if a URL is safe to fetch. Returns error message or None if safe."""
    error = _check_static_rules(url, blocklist)
    if error:
        return error
    return _check_resolved_ips(url)


@beartype
@require(lambda url: len(url) > 0, "URL must not be empty")
async def check_url_safety_async(
    url: str, blocklist: DomainBlocklist | None = None
) -> str | None:
    """Async variant of `check_url_safety`.

    Resolves DNS on the event loop's executor instead of blocking the
    loop — `socket.getaddrinfo` can stall for seconds on slow resolvers,
    which in the sync variant freezes every in-flight request/capture.
    """
    error = _check_static_rules(url, blocklist)
    if error:
        return error

    hostname = (urlparse(url).hostname or "").lower()
    loop = asyncio.get_running_loop()
    try:
        addr_infos = await loop.getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return None  # Can't resolve — allow (.onion, .i2p via proxy)
    return _evaluate_addrinfos(addr_infos)


def _check_static_rules(
    url: str, blocklist: DomainBlocklist | None
) -> str | None:
    """Run the non-resolving checks: scheme, hostname, blocklist."""
    error = _check_scheme_and_host(url)
    if error:
        return error
    if blocklist is not None:
        hostname = (urlparse(url).hostname or "").lower()
        block_reason = blocklist.check(hostname)
        if block_reason:
            from archiver.metrics import blocklist_hits_total

            blocklist_hits_total.inc()
            return block_reason
    return None


def _check_scheme_and_host(url: str) -> str | None:
    """Validate URL scheme and hostname against blocklist."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()

    if scheme not in ("http", "https"):
        return f"Unsupported scheme: {scheme}"

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return "No hostname in URL"

    if hostname in BLOCKED_HOSTNAMES:
        return f"Blocked hostname: {hostname}"

    return None


def _check_resolved_ips(url: str) -> str | None:
    """Resolve hostname and check for private/internal IPs."""
    hostname = (urlparse(url).hostname or "").lower()

    try:
        addr_infos = socket.getaddrinfo(
            hostname, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return None  # Can't resolve — allow (.onion, .i2p via proxy)
    return _evaluate_addrinfos(addr_infos)


def _evaluate_addrinfos(
    addr_infos: Sequence[
        tuple[
            int,
            int,
            int,
            str,
            tuple[str, int]
            | tuple[str, int, int, int]
            | tuple[int, bytes],
        ]
    ],
) -> str | None:
    """Check resolved addresses for private/internal IPs."""
    for _family, _, _, _, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
        ):
            return f"Blocked private/internal IP: {ip_str}"

    return None
