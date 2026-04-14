# ABOUTME: URL classification and proxy routing for Tor (.onion) and I2P (.i2p) networks
# ABOUTME: Auto-detects darknet URLs and provides the appropriate SOCKS5/HTTP proxy config
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false
"""Darknet URL classification and proxy routing."""

from __future__ import annotations

from urllib.parse import urlparse

from beartype import beartype
from icontract import ensure

from archiver.config import Settings
from archiver.enums import NetworkType
from archiver.proxy import ProxyConfig


@beartype
@ensure(
    lambda result: result in (NetworkType.CLEARNET, NetworkType.TOR, NetworkType.I2P),
    "Must return a valid NetworkType",
)
def classify_url(url: str) -> NetworkType:
    """Classify a URL as clearnet, Tor, or I2P based on its TLD."""
    host = urlparse(url).hostname or ""
    host = host.lower()

    if host.endswith(".onion"):
        return NetworkType.TOR
    if host.endswith(".i2p"):
        return NetworkType.I2P
    return NetworkType.CLEARNET


@beartype
def get_proxy_for_network(
    network: NetworkType,
    settings: Settings,
) -> ProxyConfig | None:
    """Return the appropriate proxy config for the network type."""
    if network == NetworkType.TOR:
        return ProxyConfig(server=settings.tor_proxy)
    if network == NetworkType.I2P:
        return ProxyConfig(server=settings.i2p_proxy)
    return None
