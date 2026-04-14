# ABOUTME: Unit tests for URL normalization and SHA-256 hashing
# ABOUTME: Covers tracking param removal, www stripping, port normalization, and dedup
"""Tests for URL normalization and hashing."""

from __future__ import annotations

from archiver.url import normalize_url, url_hash


class TestNormalizeUrl:
    def test_lowercases_scheme_and_host(self) -> None:
        assert normalize_url("HTTP://EXAMPLE.COM/path") == "http://example.com/path"

    def test_removes_default_http_port(self) -> None:
        assert normalize_url("http://example.com:80/path") == "http://example.com/path"

    def test_removes_default_https_port(self) -> None:
        assert normalize_url("https://example.com:443/path") == "https://example.com/path"

    def test_keeps_non_default_port(self) -> None:
        assert normalize_url("http://example.com:8080/path") == "http://example.com:8080/path"

    def test_strips_tracking_params(self) -> None:
        result = normalize_url("https://example.com/page?id=5&utm_source=twitter&fbclid=abc")
        assert result == "https://example.com/page?id=5"

    def test_sorts_query_params(self) -> None:
        result = normalize_url("https://example.com/page?z=1&a=2&m=3")
        assert result == "https://example.com/page?a=2&m=3&z=1"

    def test_removes_fragment(self) -> None:
        result = normalize_url("https://example.com/page#section")
        assert result == "https://example.com/page"

    def test_removes_trailing_slash(self) -> None:
        result = normalize_url("https://example.com/path/")
        assert result == "https://example.com/path"

    def test_keeps_root_slash(self) -> None:
        result = normalize_url("https://example.com/")
        assert result == "https://example.com/"

    def test_strips_www_by_default(self) -> None:
        result = normalize_url("https://www.example.com/page")
        assert result == "https://example.com/page"

    def test_preserves_www_when_disabled(self) -> None:
        result = normalize_url("https://www.example.com/page", strip_www=False)
        assert result == "https://www.example.com/page"

    def test_empty_path_becomes_slash(self) -> None:
        result = normalize_url("https://example.com")
        assert result == "https://example.com/"

    def test_preserves_meaningful_query_params(self) -> None:
        result = normalize_url("https://example.com/search?q=hello&page=2")
        assert "q=hello" in result
        assert "page=2" in result

    def test_variant_urls_normalize_to_same(self) -> None:
        urls = [
            "https://www.example.com/page?utm_source=twitter&id=5#top",
            "HTTPS://WWW.EXAMPLE.COM/page?id=5&utm_campaign=test",
            "https://example.com/page/?id=5&fbclid=abc123",
        ]
        normalized = {normalize_url(u) for u in urls}
        assert len(normalized) == 1


class TestUrlHash:
    def test_returns_hex_string(self) -> None:
        h = url_hash("https://example.com")
        sha256_hex_len = 64
        assert len(h) == sha256_hex_len
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_url_same_hash(self) -> None:
        assert url_hash("https://example.com") == url_hash("https://example.com")

    def test_variant_urls_same_hash(self) -> None:
        h1 = url_hash("https://www.example.com/page?utm_source=x")
        h2 = url_hash("https://example.com/page")
        assert h1 == h2

    def test_different_urls_different_hash(self) -> None:
        h1 = url_hash("https://example.com/page1")
        h2 = url_hash("https://example.com/page2")
        assert h1 != h2
