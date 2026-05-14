# ABOUTME: Unit tests for the privacy-frontend registry and URL rewriting
# ABOUTME: Covers apex resolution (exact + subdomain), rewrite preservation, misses
"""Tests for the privacy_frontends registry."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from archiver.privacy_frontends import (
    FRONTENDS,
    FrontendPolicy,
    _strip_head,
    discover_instances,
    fetch_registry_instances,
    is_alive_tcp,
    resolve_policy,
    rewrite_to_instance,
)


class TestResolvePolicy:
    def test_medium_exact_match(self) -> None:
        policy = resolve_policy("https://medium.com/@vgr/article-xyz")
        assert policy is not None
        assert policy.target_apex == "medium.com"

    def test_medium_www_match(self) -> None:
        # apex_of strips www, so www.medium.com collapses to medium.com
        policy = resolve_policy("https://www.medium.com/@vgr/foo")
        assert policy is not None
        assert policy.target_apex == "medium.com"

    def test_reddit_subdomain_match(self) -> None:
        policy = resolve_policy(
            "https://old.reddit.com/r/technology/comments/abc/"
        )
        assert policy is not None
        assert policy.target_apex == "reddit.com"

    def test_twitter_and_x_both_resolve(self) -> None:
        t = resolve_policy("https://twitter.com/jack/status/1")
        x = resolve_policy("https://x.com/jack/status/1")
        assert t is not None and t.target_apex == "twitter.com"
        assert x is not None and x.target_apex == "x.com"
        # Both should map to xcancel instances
        assert "xcancel.com" in t.instances[0]
        assert "xcancel.com" in x.instances[0]

    def test_unregistered_apex_returns_none(self) -> None:
        assert resolve_policy("https://news.ycombinator.com/") is None
        assert resolve_policy("https://github.com/foo/bar") is None
        assert resolve_policy("https://example.com/") is None

    def test_malformed_url_returns_none(self) -> None:
        assert resolve_policy("not a url") is None
        assert resolve_policy("") is None

    def test_suffix_trap_avoided(self) -> None:
        """notreddit.com should NOT match the reddit.com policy."""
        assert resolve_policy("https://notreddit.com/foo") is None


class TestRewriteToInstance:
    def test_swaps_netloc_keeps_path(self) -> None:
        result = rewrite_to_instance(
            "https://medium.com/@vgr/the-gervais-principle",
            "https://scribe.rip",
        )
        assert result == "https://scribe.rip/@vgr/the-gervais-principle"

    def test_preserves_query(self) -> None:
        result = rewrite_to_instance(
            "https://www.reddit.com/r/tech/?sort=top",
            "https://redlib.example",
        )
        assert result == "https://redlib.example/r/tech/?sort=top"

    def test_drops_fragment(self) -> None:
        """Fragments are client-only; an archive preserves none of them."""
        result = rewrite_to_instance(
            "https://twitter.com/jack/status/1#reply",
            "https://xcancel.com",
        )
        assert result == "https://xcancel.com/jack/status/1"

    def test_root_path_preserved(self) -> None:
        result = rewrite_to_instance(
            "https://medium.com/", "https://scribe.rip"
        )
        assert result == "https://scribe.rip/"

    def test_subdomain_target_flattened(self) -> None:
        """old.reddit.com/r/foo becomes INSTANCE/r/foo (subdomain dropped)."""
        result = rewrite_to_instance(
            "https://old.reddit.com/r/foo/comments/abc/",
            "https://redlib.example",
        )
        assert (
            result == "https://redlib.example/r/foo/comments/abc/"
        )


class TestRegistry:
    def test_all_policies_have_https_instances(self) -> None:
        for policy in FRONTENDS:
            assert len(policy.instances) >= 1
            for inst in policy.instances:
                assert inst.startswith("https://"), (
                    f"non-https instance: {inst} for {policy.target_apex}"
                )

    def test_apexes_are_bare(self) -> None:
        """target_apex should be the apex ('reddit.com'), not 'www.reddit.com'."""
        for policy in FRONTENDS:
            assert not policy.target_apex.startswith("www."), policy

    def test_frontend_policy_is_frozen(self) -> None:
        """Dataclass is frozen — callers can't mutate the registry at runtime."""
        p = FrontendPolicy(
            target_apex="test.com",
            instances=("https://a",),
            probe_path="/",
            probe_marker="test",
            not_found_markers=("missing",),
        )
        with pytest.raises(AttributeError):
            p.target_apex = "other.com"  # type: ignore[misc]

    def test_all_policies_have_probe_targets(self) -> None:
        """Every policy needs a probe_path + probe_marker for validation."""
        for policy in FRONTENDS:
            assert policy.probe_path.startswith("/"), policy
            assert policy.probe_marker, policy


class TestStripHead:
    def test_removes_head_block(self) -> None:
        html = (
            "<html><head><meta property='og:description' content='foo'>"
            "<title>x</title></head><body>visible</body></html>"
        )
        out = _strip_head(html)
        assert "<head" not in out
        assert "og:description" not in out
        assert "visible" in out

    def test_anubis_og_marker_does_not_leak(self) -> None:
        """The bug we're guarding against: Anubis wraps the original
        Nitter <head> (with og:description containing the tweet text)
        around its own challenge body. Naive marker-in-body passes
        false-positive; post-strip, the marker must be gone."""
        marker = "just setting up my twttr"
        anubis_page = (
            f"<html><head>"
            f"<meta property='og:description' content='{marker}'>"
            f"<title>Making sure you're not a bot!</title>"
            f"</head><body><h1>Making sure you're not a bot!</h1>"
            f"</body></html>"
        )
        stripped = _strip_head(anubis_page)
        assert marker not in stripped

    def test_real_content_survives_strip(self) -> None:
        marker = "just setting up my twttr"
        real_page = (
            "<html><head><title>jack</title></head>"
            f"<body><div class='tweet-text'>{marker}</div></body></html>"
        )
        assert marker in _strip_head(real_page)

    def test_no_head_is_passthrough(self) -> None:
        html = "<html><body>bare</body></html>"
        assert _strip_head(html) == html

    def test_case_insensitive_head(self) -> None:
        html = "<HTML><HEAD><TITLE>x</TITLE></HEAD><BODY>y</BODY></HTML>"
        out = _strip_head(html)
        assert "TITLE" not in out
        assert "y" in out


class TestIsAliveTcp:
    @pytest.mark.asyncio
    async def test_unresolvable_host_returns_false(self) -> None:
        # .invalid is reserved (RFC 6761) — guaranteed NXDOMAIN
        assert await is_alive_tcp(
            "no-such-host.invalid", timeout=2.0,
        ) is False

    @pytest.mark.asyncio
    async def test_closed_port_returns_false(self) -> None:
        # 127.0.0.1:1 is reserved & nothing listens; fast refusal
        assert await is_alive_tcp("127.0.0.1", port=1, timeout=2.0) is False

    @pytest.mark.asyncio
    async def test_rejects_invalid_port(self) -> None:
        from icontract import ViolationError

        with pytest.raises(ViolationError):
            await is_alive_tcp("example.com", port=0)
        with pytest.raises(ViolationError):
            await is_alive_tcp("example.com", port=70000)

    @pytest.mark.asyncio
    async def test_rejects_empty_host(self) -> None:
        from icontract import ViolationError

        with pytest.raises(ViolationError):
            await is_alive_tcp("", port=443)

    @pytest.mark.asyncio
    async def test_rejects_path_in_host(self) -> None:
        """Catches a common bug: passing a URL instead of a hostname."""
        from icontract import ViolationError

        with pytest.raises(ViolationError):
            await is_alive_tcp("example.com/foo", port=443)

    @pytest.mark.asyncio
    async def test_success_path_returns_true(self) -> None:
        """Mocked open_connection succeeds → writer.close + return True."""
        from unittest.mock import AsyncMock as _AsyncMock
        from unittest.mock import MagicMock as _MagicMock

        writer = _MagicMock()
        writer.close = _MagicMock()
        writer.wait_closed = _AsyncMock()
        reader = _MagicMock()

        async def fake_open_connection(*_a: object, **_kw: object) -> tuple[object, object]:
            return reader, writer

        with patch("asyncio.open_connection", side_effect=fake_open_connection):
            assert await is_alive_tcp("example.com", port=443) is True
        writer.close.assert_called_once()


class TestD420FetchFailures:
    """Cover d420-html parser's network-failure exception path."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_5xx(
        self, respx_mock,
    ) -> None:
        """Upstream 5xx → log warning + return ()."""
        import httpx

        respx_mock.get("https://test/d420").mock(
            return_value=httpx.Response(503),
        )
        p = FrontendPolicy(
            target_apex="twitter.com",
            instances=(),
            probe_path="/",
            probe_marker="x",
            registry_url="https://test/d420",
            registry_kind="d420-html",
        )
        assert await fetch_registry_instances(p) == ()




class TestRegistryHasExpandedRoster:
    def test_twitter_has_multiple_instances(self) -> None:
        """Expanded Nitter roster (status.d420.de) should give us a
        proper fallback chain, not a single point of failure."""
        min_fallback_instances = 5
        t = resolve_policy("https://twitter.com/jack/status/20")
        assert t is not None
        assert len(t.instances) >= min_fallback_instances, (
            "twitter.com policy should list multiple Nitter mirrors"
        )

    def test_twitter_and_x_share_roster(self) -> None:
        """twitter.com and x.com both target the same Nitter family —
        same probe target so we don't get drift between the two."""
        t = resolve_policy("https://twitter.com/jack/status/20")
        x = resolve_policy("https://x.com/jack/status/20")
        assert t is not None and x is not None
        assert t.instances == x.instances
        assert t.probe_path == x.probe_path
        assert t.probe_marker == x.probe_marker


class TestRegistryDiscovery:
    """Cover the upstream-registry instance discovery layer."""

    @pytest.mark.asyncio
    async def test_no_registry_returns_empty(self) -> None:
        """A policy without registry_url contributes no extra instances."""
        p = FrontendPolicy(
            target_apex="example.com",
            instances=("https://a",),
            probe_path="/",
            probe_marker="x",
        )
        assert await fetch_registry_instances(p) == ()

    @pytest.mark.asyncio
    async def test_discover_falls_back_to_static_on_registry_failure(
        self, respx_mock,  # pytest fixture
    ) -> None:
        """A registry fetch error should NOT break discovery — we still
        get the hardcoded fallback list."""
        import httpx

        respx_mock.get("https://registry.invalid/list").mock(
            return_value=httpx.Response(500),
        )
        p = FrontendPolicy(
            target_apex="example.com",
            instances=("https://a", "https://b"),
            probe_path="/",
            probe_marker="x",
            registry_url="https://registry.invalid/list",
            registry_kind="redlib-json",
        )
        result = await discover_instances(p)
        assert result == ("https://a", "https://b")

    @pytest.mark.asyncio
    async def test_redlib_json_parser_drops_onion_and_keeps_https(
        self, respx_mock,
    ) -> None:
        """redlib-json schema: drop onion-only entries, keep https ones,
        strip trailing slashes."""
        import httpx

        body = {
            "updated": "2026-05-13",
            "instances": [
                {"url": "https://a.example/", "country": "DE"},
                {"url": "https://b.example", "country": "US"},
                # onion-only — must be skipped
                {"onion": "http://abc.onion", "country": "DE"},
                # http (non-https) — must be skipped for safety
                {"url": "http://insecure.example", "country": "US"},
            ],
        }
        respx_mock.get("https://test/redlib.json").mock(
            return_value=httpx.Response(200, json=body),
        )
        p = FrontendPolicy(
            target_apex="reddit.com",
            instances=(),
            probe_path="/",
            probe_marker="x",
            registry_url="https://test/redlib.json",
            registry_kind="redlib-json",
        )
        result = await fetch_registry_instances(p)
        assert result == ("https://a.example", "https://b.example")

    @pytest.mark.asyncio
    async def test_d420_html_parser_extracts_instances(
        self, respx_mock,
    ) -> None:
        """d420 HTML pattern: one <a rel="nofollow external"
        href="https://..."> per instance, mixed with github metadata
        links that share the same rel and must be filtered out."""
        import httpx

        html = """
            <html><body>
            <a rel="nofollow external" href="https://nitter.foo">x</a>
            <a rel="nofollow external" href="https://github.com/zedeus/nitter/commit/abc">link</a>
            <a rel="nofollow external" href="https://nitter.bar">y</a>
            <a rel="nofollow external" href="https://en.wikipedia.org/wiki/X">wiki</a>
            <a rel="nofollow external" href="https://nitter.foo">dup</a>
            </body></html>
        """
        respx_mock.get("https://test/d420").mock(
            return_value=httpx.Response(200, text=html),
        )
        p = FrontendPolicy(
            target_apex="twitter.com",
            instances=(),
            probe_path="/",
            probe_marker="x",
            registry_url="https://test/d420",
            registry_kind="d420-html",
        )
        result = await fetch_registry_instances(p)
        # github + wikipedia filtered, duplicate collapsed
        assert result == ("https://nitter.foo", "https://nitter.bar")

    @pytest.mark.asyncio
    async def test_discover_dedupes_and_preserves_order(
        self, respx_mock,
    ) -> None:
        """Static instances come first (preference order); registry
        additions append. Duplicates in either list collapse."""
        import httpx

        respx_mock.get("https://test/r.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "instances": [
                        # First two duplicate the static list
                        {"url": "https://b.example"},
                        {"url": "https://a.example"},
                        # New one
                        {"url": "https://c.example"},
                    ],
                },
            ),
        )
        p = FrontendPolicy(
            target_apex="example.com",
            instances=("https://a.example", "https://b.example"),
            probe_path="/",
            probe_marker="x",
            registry_url="https://test/r.json",
            registry_kind="redlib-json",
        )
        result = await discover_instances(p)
        assert result == (
            "https://a.example",
            "https://b.example",
            "https://c.example",
        )

    @pytest.mark.asyncio
    async def test_unknown_registry_kind_returns_empty(self) -> None:
        """Defensive: bad config never crashes the probe loop."""
        p = FrontendPolicy(
            target_apex="example.com",
            instances=("https://a",),
            probe_path="/",
            probe_marker="x",
            registry_url="https://test/x",
            registry_kind="totally-fake-kind",
        )
        assert await fetch_registry_instances(p) == ()
