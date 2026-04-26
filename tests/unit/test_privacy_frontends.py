# ABOUTME: Unit tests for the privacy-frontend registry and URL rewriting
# ABOUTME: Covers apex resolution (exact + subdomain), rewrite preservation, misses
"""Tests for the privacy_frontends registry."""

from __future__ import annotations

import pytest

from archiver.privacy_frontends import (
    FRONTENDS,
    FrontendPolicy,
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
