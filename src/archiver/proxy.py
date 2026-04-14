# ABOUTME: Proxy list management with round-robin rotation and health tracking
# ABOUTME: Provides proxy configs for Tier 3 (custom proxies), Tor, and I2P capture
# pyright: reportUnknownVariableType=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
"""Proxy rotation for capture tiers."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import structlog
from beartype import beartype
from icontract import require

log = structlog.get_logger()


@dataclass(frozen=True)
class ProxyConfig:
    """A single proxy endpoint."""

    server: str  # protocol://host:port


@dataclass
class ProxyRotator:
    """Round-robin proxy selection with failure tracking."""

    proxies: list[ProxyConfig] = field(default_factory=list)
    _failed: set[str] = field(default_factory=set, repr=False)
    _cycle: itertools.cycle[ProxyConfig] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.proxies:
            self._cycle = itertools.cycle(self.proxies)

    @beartype
    def next(self) -> ProxyConfig | None:
        """Get the next available proxy, skipping failed ones."""
        if not self.proxies or self._cycle is None:
            return None

        # Try up to len(proxies) times to find a non-failed one
        for _ in range(len(self.proxies)):
            proxy = next(self._cycle)
            if proxy.server not in self._failed:
                return proxy

        # All failed — reset and try first
        self._failed.clear()
        log.warning("proxy.all_failed_reset")
        return next(self._cycle)

    @beartype
    def mark_failed(self, proxy: ProxyConfig) -> None:
        """Mark a proxy as failed."""
        self._failed.add(proxy.server)
        log.warning("proxy.marked_failed", server=proxy.server)

    @beartype
    def mark_success(self, proxy: ProxyConfig) -> None:
        """Clear failure status for a proxy."""
        self._failed.discard(proxy.server)

    @property
    def available_count(self) -> int:
        """Number of non-failed proxies."""
        return len(self.proxies) - len(self._failed)


@beartype
@require(
    lambda proxy_list: isinstance(proxy_list, str),
    "proxy_list must be a string",
)
def parse_proxy_list(proxy_list: str) -> list[ProxyConfig]:
    """Parse comma-separated proxy list or file path into ProxyConfig list."""
    if not proxy_list.strip():
        return []

    # If it looks like a file path, read it
    from pathlib import Path

    path = Path(proxy_list.strip())
    if path.exists() and path.is_file():
        lines = path.read_text().strip().splitlines()
    else:
        lines = [s.strip() for s in proxy_list.split(",")]

    return [
        ProxyConfig(server=line.strip())
        for line in lines
        if line.strip()
    ]
