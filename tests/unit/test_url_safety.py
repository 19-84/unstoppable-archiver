# ABOUTME: Unit tests for SSRF protection in URL safety checks
# ABOUTME: Verifies private IPs, internal hostnames, and allowed URLs
"""Tests for URL safety checks."""

from __future__ import annotations

from archiver.url_safety import check_url_safety, check_url_safety_async


class TestCheckUrlSafety:
    def test_allows_normal_https(self) -> None:
        assert check_url_safety("https://example.com") is None

    def test_allows_normal_http(self) -> None:
        assert check_url_safety("http://example.com") is None

    def test_blocks_file_scheme(self) -> None:
        result = check_url_safety("file:///etc/passwd")
        assert result is not None
        assert "scheme" in result.lower()

    def test_blocks_javascript_scheme(self) -> None:
        result = check_url_safety("javascript:alert(1)")
        assert result is not None

    def test_blocks_localhost(self) -> None:
        result = check_url_safety("http://localhost:8080/")
        assert result is not None
        assert "Blocked" in result

    def test_blocks_postgres_hostname(self) -> None:
        result = check_url_safety("http://postgres:5432/")
        assert result is not None

    def test_blocks_docker_internal(self) -> None:
        for host in ["tor", "i2p", "worker", "app", "redis"]:
            result = check_url_safety(f"http://{host}/")
            assert result is not None, f"{host} should be blocked"

    def test_blocks_loopback_ip(self) -> None:
        result = check_url_safety("http://127.0.0.1/")
        assert result is not None
        assert "private" in result.lower() or "Blocked" in result

    def test_blocks_metadata_ip(self) -> None:
        result = check_url_safety("http://169.254.169.254/latest/")
        assert result is not None

    def test_blocks_private_10_range(self) -> None:
        result = check_url_safety("http://10.0.0.1/")
        assert result is not None

    def test_blocks_private_172_range(self) -> None:
        result = check_url_safety("http://172.16.0.1/")
        assert result is not None

    def test_blocks_private_192_range(self) -> None:
        result = check_url_safety("http://192.168.1.1/")
        assert result is not None

    def test_allows_onion(self) -> None:
        # .onion can't be resolved via DNS — should pass
        result = check_url_safety(
            "http://expyuzz4wqqyqhjn.onion/"
        )
        assert result is None

    def test_blocks_empty_hostname(self) -> None:
        result = check_url_safety("http:///path")
        assert result is not None

    def test_blocks_zero_ip(self) -> None:
        result = check_url_safety("http://0.0.0.0/")
        assert result is not None

    def test_domain_blocklist_hit_reported(self) -> None:
        """A passed-in blocklist should intercept before DNS resolution."""
        from archiver.blocklist import DomainBlocklist

        bl = DomainBlocklist(blocked={"evil.example.com"})
        result = check_url_safety(
            "https://evil.example.com/", blocklist=bl
        )
        assert result is not None
        assert "evil.example.com" in result


class TestCheckUrlSafetyAsync:
    """Async variant — same rules, DNS resolution off the event loop."""

    async def test_allows_normal_https(self) -> None:
        assert await check_url_safety_async("https://example.com") is None

    async def test_blocks_file_scheme(self) -> None:
        result = await check_url_safety_async("file:///etc/passwd")
        assert result is not None
        assert "scheme" in result.lower()

    async def test_blocks_docker_hostname(self) -> None:
        result = await check_url_safety_async("http://postgres:5432/")
        assert result is not None

    async def test_blocks_loopback_ip(self) -> None:
        result = await check_url_safety_async("http://127.0.0.1/")
        assert result is not None
        assert "private" in result.lower() or "Blocked" in result

    async def test_blocks_metadata_ip(self) -> None:
        result = await check_url_safety_async("http://169.254.169.254/")
        assert result is not None

    async def test_allows_onion(self) -> None:
        result = await check_url_safety_async(
            "http://expyuzz4wqqyqhjn.onion/"
        )
        assert result is None

    async def test_domain_blocklist_hit_reported(self) -> None:
        from archiver.blocklist import DomainBlocklist

        bl = DomainBlocklist(blocked={"evil.example.com"})
        result = await check_url_safety_async(
            "https://evil.example.com/", blocklist=bl
        )
        assert result is not None
        assert "evil.example.com" in result
