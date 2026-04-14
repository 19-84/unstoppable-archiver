# ABOUTME: Unit tests for darknet URL classification and proxy routing
# ABOUTME: Verifies .onion -> Tor, .i2p -> I2P, and clearnet routing logic
"""Tests for darknet URL classification."""

from __future__ import annotations

from archiver.config import Settings
from archiver.darknet import classify_url, get_proxy_for_network
from archiver.enums import NetworkType


class TestClassifyUrl:
    def test_onion_url(self) -> None:
        assert (
            classify_url(
                "http://expyuzz4wqqyqhjn.onion/wiki/Main_Page"
            )
            == NetworkType.TOR
        )

    def test_i2p_url(self) -> None:
        assert (
            classify_url("http://git.idk.i2p/")
            == NetworkType.I2P
        )

    def test_clearnet_url(self) -> None:
        assert (
            classify_url("https://example.com")
            == NetworkType.CLEARNET
        )

    def test_https_onion(self) -> None:
        assert (
            classify_url("https://hidden.onion/path")
            == NetworkType.TOR
        )

    def test_subdomain_onion(self) -> None:
        assert (
            classify_url("http://sub.hidden.onion")
            == NetworkType.TOR
        )

    def test_not_onion_suffix(self) -> None:
        assert (
            classify_url("https://onion.example.com")
            == NetworkType.CLEARNET
        )


class TestGetProxyForNetwork:
    def test_tor_returns_socks5(self) -> None:
        settings = Settings(
            tor_proxy="socks5://tor:9050",
            i2p_proxy="http://i2p:4444",
        )
        proxy = get_proxy_for_network(
            NetworkType.TOR, settings
        )
        assert proxy is not None
        assert proxy.server == "socks5://tor:9050"

    def test_i2p_returns_http(self) -> None:
        settings = Settings(
            tor_proxy="socks5://tor:9050",
            i2p_proxy="http://i2p:4444",
        )
        proxy = get_proxy_for_network(
            NetworkType.I2P, settings
        )
        assert proxy is not None
        assert proxy.server == "http://i2p:4444"

    def test_clearnet_returns_none(self) -> None:
        settings = Settings()
        proxy = get_proxy_for_network(
            NetworkType.CLEARNET, settings
        )
        assert proxy is None
