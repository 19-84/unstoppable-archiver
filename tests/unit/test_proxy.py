# ABOUTME: Unit tests for proxy rotation and list parsing
# ABOUTME: Tests round-robin selection, failure tracking, reset behavior, and file/string parsing
"""Tests for proxy rotation."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from archiver.proxy import (
    ProxyConfig,
    ProxyRotator,
    _asn_lookup_cache,
    _infer_scheme_from_url,
    _normalize_entry,
    fetch_proxy_list_url,
    filter_by_asn,
    filter_healthy,
    filter_socks5,
    health_check_proxy,
    is_datacenter_org,
    load_proxies,
    lookup_asn,
    parse_proxy_list,
)


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

    def test_bare_host_port_gets_default_scheme(self) -> None:
        result = parse_proxy_list("1.2.3.4:8080", default_scheme="socks5")
        assert result[0].server == "socks5://1.2.3.4:8080"

    def test_skips_comment_lines(self, tmp_path: Path) -> None:
        proxy_file = tmp_path / "p.txt"
        proxy_file.write_text("# header\nhttp://p1:8080\n# another\n")
        result = parse_proxy_list(str(proxy_file))
        assert len(result) == 1

    def test_deduplicates(self) -> None:
        result = parse_proxy_list(
            "http://p1:8080,http://p1:8080,http://p2:8080"
        )
        assert len(result) == 2  # noqa: PLR2004

    def test_invalid_scheme_skipped(self) -> None:
        result = parse_proxy_list("ftp://p1:8080,http://p2:8080")
        assert len(result) == 1
        assert result[0].server == "http://p2:8080"

    def test_malformed_entry_skipped(self) -> None:
        result = parse_proxy_list("not-a-proxy,http://p1:8080")
        assert len(result) == 1


class TestNormalizeEntry:
    def test_bare_host_port(self) -> None:
        assert _normalize_entry("1.2.3.4:80", "http") == "http://1.2.3.4:80"

    def test_preserves_scheme(self) -> None:
        assert (
            _normalize_entry("socks5://1.2.3.4:1080", "http")
            == "socks5://1.2.3.4:1080"
        )

    def test_non_numeric_port_rejected(self) -> None:
        assert _normalize_entry("1.2.3.4:abc", "http") is None

    def test_path_in_entry_rejected(self) -> None:
        assert _normalize_entry("1.2.3.4:80/path", "http") is None

    def test_trailing_country_field_stripped(self) -> None:
        # zloi-user/hideip.me format: host:port:country
        assert (
            _normalize_entry("1.2.3.4:1080:United States", "socks5")
            == "socks5://1.2.3.4:1080"
        )


class TestInferScheme:
    def test_http_txt_filename(self) -> None:
        assert _infer_scheme_from_url(
            "https://raw.githubusercontent.com/user/repo/master/http.txt"
        ) == "http"

    def test_socks5_txt_filename(self) -> None:
        assert _infer_scheme_from_url("https://host/proxies/socks5.txt") == "socks5"

    def test_socks5_path_segment(self) -> None:
        # proxifly: .../protocols/socks5/data.txt
        assert _infer_scheme_from_url(
            "https://host/repo/main/protocols/socks5/data.txt"
        ) == "socks5"

    def test_socks5_underscore_dir(self) -> None:
        # hookzof: .../socks5_list/master/proxy.txt
        assert _infer_scheme_from_url(
            "https://host/hookzof/socks5_list/master/proxy.txt"
        ) == "socks5"

    def test_unknown_returns_none(self) -> None:
        assert _infer_scheme_from_url("https://example.com/list") is None


class TestFetchProxyListUrl:
    @respx.mock
    async def test_fetches_and_parses(self) -> None:
        respx.get("https://host/proxies/socks5.txt").mock(
            return_value=httpx.Response(
                200, text="1.2.3.4:1080\n5.6.7.8:1080\n"
            )
        )
        result = await fetch_proxy_list_url(
            "https://host/proxies/socks5.txt"
        )
        assert len(result) == 2  # noqa: PLR2004
        assert all(r.server.startswith("socks5://") for r in result)

    @respx.mock
    async def test_404_returns_empty(self) -> None:
        respx.get("https://host/missing.txt").mock(
            return_value=httpx.Response(404)
        )
        result = await fetch_proxy_list_url("https://host/missing.txt")
        assert result == []

    @respx.mock
    async def test_network_error_returns_empty(self) -> None:
        respx.get("https://host/err.txt").mock(
            side_effect=httpx.ConnectError("boom")
        )
        result = await fetch_proxy_list_url("https://host/err.txt")
        assert result == []


class TestLoadProxies:
    @respx.mock
    async def test_unions_inline_and_url(self) -> None:
        respx.get("https://host/http.txt").mock(
            return_value=httpx.Response(200, text="1.1.1.1:80\n2.2.2.2:80")
        )
        result = await load_proxies(
            proxy_list="http://local:8080",
            proxy_list_urls="https://host/http.txt",
        )
        assert len(result) == 3  # noqa: PLR2004

    async def test_empty_config_returns_empty(self) -> None:
        result = await load_proxies(proxy_list="", proxy_list_urls="")
        assert result == []

    @respx.mock
    async def test_respects_max_count(self) -> None:
        body = "\n".join(f"10.0.0.{i}:80" for i in range(1, 20))
        respx.get("https://host/http.txt").mock(
            return_value=httpx.Response(200, text=body)
        )
        result = await load_proxies(
            proxy_list="", proxy_list_urls="https://host/http.txt",
            max_count=5,
        )
        assert len(result) == 5  # noqa: PLR2004

    @respx.mock
    async def test_zero_max_count_means_no_cap(self) -> None:
        body = "\n".join(f"10.0.0.{i}:80" for i in range(1, 20))
        respx.get("https://host/http.txt").mock(
            return_value=httpx.Response(200, text=body)
        )
        result = await load_proxies(
            proxy_list="", proxy_list_urls="https://host/http.txt",
            max_count=0,
        )
        assert len(result) == 19  # all entries preserved  # noqa: PLR2004


class TestHealthCheck:
    @respx.mock
    async def test_proxy_health_ok(self) -> None:
        # With `proxy=...` httpx routes through the proxy host but respx
        # intercepts at the logical URL level, so mocking the probe URL
        # suffices.
        respx.get("https://probe.example/ip").mock(
            return_value=httpx.Response(200, json={"ip": "1.2.3.4"})
        )
        ok = await health_check_proxy(
            ProxyConfig(server="http://proxy:8080"),
            "https://probe.example/ip",
        )
        assert ok is True

    @respx.mock
    async def test_proxy_health_failure(self) -> None:
        respx.get("https://probe.example/ip").mock(
            return_value=httpx.Response(502)
        )
        ok = await health_check_proxy(
            ProxyConfig(server="http://proxy:8080"),
            "https://probe.example/ip",
        )
        assert ok is False

    @respx.mock
    async def test_proxy_health_network_failure(self) -> None:
        """Connection errors are categorized as unhealthy, not re-raised."""
        respx.get("https://probe.example/ip").mock(
            side_effect=httpx.ConnectError("proxy unreachable")
        )
        ok = await health_check_proxy(
            ProxyConfig(server="http://proxy:8080"),
            "https://probe.example/ip",
        )
        assert ok is False

    async def test_proxy_health_import_error_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Systemic config problems (missing httpx[socks]) return False."""
        import httpx as _httpx

        orig_client = _httpx.AsyncClient

        def exploder(*args: object, **kwargs: object) -> object:
            raise ImportError("httpx[socks] not installed")

        monkeypatch.setattr(_httpx, "AsyncClient", exploder)
        try:
            ok = await health_check_proxy(
                ProxyConfig(server="socks5://bad:1080"),
                "https://probe.example/ip",
            )
            assert ok is False
        finally:
            monkeypatch.setattr(_httpx, "AsyncClient", orig_client)

    @respx.mock
    async def test_filter_healthy_partitions(self) -> None:
        respx.get("https://probe.example/ip").mock(
            side_effect=[
                httpx.Response(200, json={"ip": "a"}),
                httpx.Response(500),
                httpx.Response(200, json={"ip": "b"}),
            ]
        )
        proxies = [
            ProxyConfig(server="http://p1:8080"),
            ProxyConfig(server="http://p2:8080"),
            ProxyConfig(server="http://p3:8080"),
        ]
        healthy = await filter_healthy(
            proxies,
            probe_url="https://probe.example/ip",
            concurrency=1,
        )
        # With concurrency=1 the side_effect ordering matches input order.
        assert len(healthy) == 2  # noqa: PLR2004

    async def test_filter_healthy_empty(self) -> None:
        healthy = await filter_healthy(
            [], probe_url="https://probe/ip"
        )
        assert healthy == []


class TestIsDatacenterOrg:
    def test_hetzner_detected(self) -> None:
        assert is_datacenter_org("Hetzner Online GmbH") is True

    def test_aws_detected(self) -> None:
        assert is_datacenter_org("Amazon.com, Inc.") is True
        assert is_datacenter_org("AWS EC2 us-east-1") is True

    def test_google_cloud_detected(self) -> None:
        assert is_datacenter_org("Google LLC") is True
        assert is_datacenter_org("google cloud platform") is True

    def test_consumer_isp_not_flagged(self) -> None:
        assert is_datacenter_org("Comcast Cable") is False
        assert is_datacenter_org("MTS PJSC") is False
        assert is_datacenter_org("Dogado GmbH") is False
        assert is_datacenter_org("Ix Telecom") is False

    def test_empty_string_not_flagged(self) -> None:
        assert is_datacenter_org("") is False


class TestFilterSocks5:
    def test_keeps_only_socks5(self) -> None:
        proxies = [
            ProxyConfig(server="http://h1:8080"),
            ProxyConfig(server="socks5://h2:1080"),
            ProxyConfig(server="socks4://h3:1080"),
            ProxyConfig(server="https://h4:443"),
            ProxyConfig(server="socks5://h5:9050"),
        ]
        kept = filter_socks5(proxies)
        assert [p.server for p in kept] == [
            "socks5://h2:1080", "socks5://h5:9050",
        ]

    def test_empty(self) -> None:
        assert filter_socks5([]) == []


class TestAsnLookup:
    def setup_method(self) -> None:
        _asn_lookup_cache.clear()

    @respx.mock
    async def test_lookup_returns_org_and_country(self) -> None:
        respx.get("https://ipwho.is/1.2.3.4").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "country_code": "DE",
                "connection": {"org": "Hetzner Online", "isp": "Hetzner"},
            })
        )
        info = await lookup_asn("1.2.3.4")
        assert info["org"] == "Hetzner Online"
        assert info["country"] == "DE"

    @respx.mock
    async def test_lookup_caches(self) -> None:
        call_count = {"n": 0}

        def responder(_: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={
                "success": True,
                "country_code": "US",
                "connection": {"org": "X", "isp": "Y"},
            })

        respx.get("https://ipwho.is/5.6.7.8").mock(side_effect=responder)
        await lookup_asn("5.6.7.8")
        await lookup_asn("5.6.7.8")
        assert call_count["n"] == 1

    @respx.mock
    async def test_lookup_handles_failure(self) -> None:
        respx.get("https://ipwho.is/9.9.9.9").mock(
            return_value=httpx.Response(200, json={"success": False})
        )
        assert await lookup_asn("9.9.9.9") == {}

    @respx.mock
    async def test_lookup_http_error_returns_empty(self) -> None:
        respx.get("https://ipwho.is/8.8.8.8").mock(
            return_value=httpx.Response(500)
        )
        assert await lookup_asn("8.8.8.8") == {}

    @respx.mock
    async def test_lookup_exception_returns_empty(self) -> None:
        respx.get("https://ipwho.is/7.7.7.7").mock(
            side_effect=httpx.ConnectError("boom")
        )
        assert await lookup_asn("7.7.7.7") == {}


class TestProbeArchiveGate:
    def _fake_camoufox(self, html: str) -> object:
        """Build a mock AsyncCamoufox ctx manager that yields a page returning `html`."""
        from unittest.mock import AsyncMock, MagicMock

        page = AsyncMock()
        page.goto = AsyncMock()
        page.content = AsyncMock(return_value=html)
        page.close = AsyncMock()
        context = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()
        browser = AsyncMock()
        browser.new_context = AsyncMock(return_value=context)
        camoufox = MagicMock()
        camoufox.__aenter__ = AsyncMock(return_value=browser)
        camoufox.__aexit__ = AsyncMock(return_value=False)
        return camoufox

    async def test_clean_response_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Response lacking reCAPTCHA markers → gate considered passed."""
        from camoufox import async_api

        cx = self._fake_camoufox("<html><body>Welcome to archive.ph</body></html>")
        monkeypatch.setattr(
            async_api, "AsyncCamoufox", lambda *a, **kw: cx
        )
        from archiver.proxy import probe_archive_gate
        assert await probe_archive_gate(ProxyConfig(server="socks5://p:1")) is True

    async def test_recaptcha_response_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from camoufox import async_api

        challenge = '<html><div id="g-recaptcha"></div></html>'
        cx = self._fake_camoufox(challenge)
        monkeypatch.setattr(
            async_api, "AsyncCamoufox", lambda *a, **kw: cx
        )
        from archiver.proxy import probe_archive_gate
        assert await probe_archive_gate(ProxyConfig(server="socks5://p:1")) is False

    async def test_exception_classifies_as_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from camoufox import async_api

        def exploder(*a: object, **kw: object) -> object:
            raise RuntimeError("camoufox failed")

        monkeypatch.setattr(async_api, "AsyncCamoufox", exploder)
        from archiver.proxy import probe_archive_gate
        assert await probe_archive_gate(ProxyConfig(server="socks5://p:1")) is False


class TestFilterGatePassing:
    async def test_filters_using_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """filter_gate_passing should use probe_archive_gate results."""
        results = {
            "socks5://good:1": True,
            "socks5://bad:1": False,
            "socks5://flaky:1": True,
        }

        async def fake_probe(p: ProxyConfig, timeout: float = 45.0) -> bool:
            return results[p.server]

        from archiver import proxy as proxy_mod
        monkeypatch.setattr(proxy_mod, "probe_archive_gate", fake_probe)

        proxies = [
            ProxyConfig(server=s) for s in results
        ]
        kept = await proxy_mod.filter_gate_passing(proxies, concurrency=3)
        assert sorted(p.server for p in kept) == [
            "socks5://flaky:1", "socks5://good:1",
        ]

    async def test_empty(self) -> None:
        from archiver.proxy import filter_gate_passing
        assert await filter_gate_passing([]) == []


class TestFilterByAsn:
    def setup_method(self) -> None:
        _asn_lookup_cache.clear()

    @respx.mock
    async def test_drops_datacenter_keeps_residential(self) -> None:
        respx.get("https://ipwho.is/1.1.1.1").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "country_code": "DE",
                "connection": {"org": "Hetzner Online", "isp": "Hetzner"},
            })
        )
        respx.get("https://ipwho.is/2.2.2.2").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "country_code": "RU",
                "connection": {"org": "MTS PJSC", "isp": "MTS"},
            })
        )
        respx.get("https://ipwho.is/3.3.3.3").mock(
            return_value=httpx.Response(200, json={
                "success": True,
                "country_code": "US",
                "connection": {"org": "Amazon AWS", "isp": "AWS"},
            })
        )
        proxies = [
            ProxyConfig(server="socks5://1.1.1.1:1080"),
            ProxyConfig(server="socks5://2.2.2.2:1080"),
            ProxyConfig(server="http://3.3.3.3:8080"),
        ]
        kept = await filter_by_asn(proxies)
        assert [p.server for p in kept] == ["socks5://2.2.2.2:1080"]

    async def test_empty_returns_empty(self) -> None:
        assert await filter_by_asn([]) == []
