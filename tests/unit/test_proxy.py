# ABOUTME: Unit tests for proxy rotation and list parsing
# ABOUTME: Tests round-robin selection, failure tracking, reset behavior, and file/string parsing
"""Tests for proxy rotation."""

from __future__ import annotations

from pathlib import Path

from archiver.proxy import ProxyConfig, ProxyRotator, parse_proxy_list


class TestProxyRotator:
    def test_empty_returns_none(self) -> None:
        rotator = ProxyRotator()
        assert rotator.next() is None

    def test_single_proxy_returns_it(self) -> None:
        rotator = ProxyRotator(
            proxies=[ProxyConfig(server="http://p1:8080")]
        )
        result = rotator.next()
        assert result is not None
        assert result.server == "http://p1:8080"

    def test_round_robin(self) -> None:
        proxies = [
            ProxyConfig(server="http://p1:8080"),
            ProxyConfig(server="http://p2:8080"),
            ProxyConfig(server="http://p3:8080"),
        ]
        rotator = ProxyRotator(proxies=proxies)
        servers = [rotator.next().server for _ in range(6)]  # type: ignore[union-attr]
        assert servers == [
            "http://p1:8080",
            "http://p2:8080",
            "http://p3:8080",
            "http://p1:8080",
            "http://p2:8080",
            "http://p3:8080",
        ]

    def test_skips_failed(self) -> None:
        proxies = [
            ProxyConfig(server="http://p1:8080"),
            ProxyConfig(server="http://p2:8080"),
        ]
        rotator = ProxyRotator(proxies=proxies)
        rotator.mark_failed(proxies[0])

        result = rotator.next()
        assert result is not None
        assert result.server == "http://p2:8080"

    def test_all_failed_resets(self) -> None:
        proxies = [ProxyConfig(server="http://p1:8080")]
        rotator = ProxyRotator(proxies=proxies)
        rotator.mark_failed(proxies[0])

        result = rotator.next()
        assert result is not None  # Reset happens, returns proxy

    def test_mark_success_clears_failure(self) -> None:
        proxy = ProxyConfig(server="http://p1:8080")
        rotator = ProxyRotator(proxies=[proxy])
        rotator.mark_failed(proxy)
        assert rotator.available_count == 0
        rotator.mark_success(proxy)
        assert rotator.available_count == 1

    def test_available_count(self) -> None:
        proxies = [
            ProxyConfig(server="http://p1:8080"),
            ProxyConfig(server="http://p2:8080"),
        ]
        rotator = ProxyRotator(proxies=proxies)
        assert rotator.available_count == 2  # noqa: PLR2004
        rotator.mark_failed(proxies[0])
        assert rotator.available_count == 1


class TestParseProxyList:
    def test_empty_string(self) -> None:
        assert parse_proxy_list("") == []

    def test_comma_separated(self) -> None:
        result = parse_proxy_list(
            "http://p1:8080, socks5://p2:1080"
        )
        assert len(result) == 2  # noqa: PLR2004
        assert result[0].server == "http://p1:8080"
        assert result[1].server == "socks5://p2:1080"

    def test_from_file(self, tmp_path: Path) -> None:
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "http://p1:8080\nsocks5://p2:1080\n"
        )
        result = parse_proxy_list(str(proxy_file))
        assert len(result) == 2  # noqa: PLR2004

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        proxy_file = tmp_path / "proxies.txt"
        proxy_file.write_text(
            "http://p1:8080\n\n\nhttp://p2:8080\n"
        )
        result = parse_proxy_list(str(proxy_file))
        assert len(result) == 2  # noqa: PLR2004
