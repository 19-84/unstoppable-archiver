# ABOUTME: Tests for domain blocklist with allowlist overrides
# ABOUTME: Covers hosts file parsing, subdomain matching, longest-match-wins semantics
"""Tests for blocklist module."""

from __future__ import annotations

import httpx
import pytest
import respx

from archiver.blocklist import (
    DomainBlocklist,
    _parse_domain_list,
    _walk_up,
    load_blocklist,
)
from archiver.config import Settings


class TestParseDomainList:
    def test_plain_list(self) -> None:
        content = "example.com\nfoo.bar\n# comment\n\nbaz.test"
        result = _parse_domain_list(content)
        assert result == {"example.com", "foo.bar", "baz.test"}

    def test_hosts_format(self) -> None:
        content = "0.0.0.0 example.com\n127.0.0.1 bad.test\n"
        result = _parse_domain_list(content)
        assert result == {"example.com", "bad.test"}

    def test_strips_www_prefix(self) -> None:
        content = "www.example.com"
        assert _parse_domain_list(content) == {"example.com"}

    def test_ignores_invalid_entries(self) -> None:
        # No dot, or contains slash → rejected
        content = "no-dot-here\n/path/is-bad"
        assert _parse_domain_list(content) == set()

    def test_ignores_comments(self) -> None:
        content = "example.com # inline comment\n# full line comment\nfoo.bar"
        assert _parse_domain_list(content) == {"example.com", "foo.bar"}


class TestWalkUp:
    def test_three_labels(self) -> None:
        assert _walk_up("a.b.example.com") == [
            "a.b.example.com", "b.example.com", "example.com",
        ]

    def test_two_labels(self) -> None:
        assert _walk_up("example.com") == ["example.com"]

    def test_skips_tld_alone(self) -> None:
        # We don't want "com" to match the blocklist even if "com" is in it
        assert "com" not in _walk_up("example.com")


class TestDomainBlocklistCheck:
    def test_exact_match(self) -> None:
        bl = DomainBlocklist(blocked={"bad.com"})
        assert bl.check("bad.com") == "Domain blocked: bad.com"

    def test_subdomain_blocked(self) -> None:
        bl = DomainBlocklist(blocked={"bad.com"})
        assert bl.check("sub.bad.com") == "Domain blocked: bad.com"

    def test_deep_subdomain_blocked(self) -> None:
        bl = DomainBlocklist(blocked={"bad.com"})
        assert bl.check("a.b.c.bad.com") == "Domain blocked: bad.com"

    def test_unrelated_allowed(self) -> None:
        bl = DomainBlocklist(blocked={"bad.com"})
        assert bl.check("good.com") is None

    def test_empty_hostname(self) -> None:
        bl = DomainBlocklist(blocked={"bad.com"})
        assert bl.check("") is None

    def test_www_prefix_normalized(self) -> None:
        bl = DomainBlocklist(blocked={"bad.com"})
        assert bl.check("www.bad.com") is not None

    def test_case_insensitive(self) -> None:
        bl = DomainBlocklist(blocked={"bad.com"})
        assert bl.check("SUB.BAD.COM") is not None


class TestAllowlistOverride:
    def test_allowlist_unblocks_subdomain(self) -> None:
        """good.example.com in allowlist; example.com in blocklist."""
        bl = DomainBlocklist(
            blocked={"example.com"},
            allowed={"good.example.com"},
        )
        assert bl.check("good.example.com") is None

    def test_blocklist_takes_specific_subdomain(self) -> None:
        """example.com allowed; bad.example.com blocked."""
        bl = DomainBlocklist(
            blocked={"bad.example.com"},
            allowed={"example.com"},
        )
        assert bl.check("bad.example.com") is not None

    def test_apex_allowlist_unblocks_all_subs(self) -> None:
        """Allowlist apex also frees any subdomain (unless specifically blocked)."""
        bl = DomainBlocklist(
            blocked={"example.com"},
            allowed={"example.com"},  # same domain in both
        )
        # Longest match equal → allowlist wins (checked first in order)
        assert bl.check("example.com") is None


class TestLoadBlocklist:
    async def test_load_from_inline_domains(self, tmp_path: pytest.Path) -> None:  # type: ignore[name-defined]
        settings = Settings(
            blocklist_domains="spam.test,scam.example",
            allowlist_domains="good.test",
        )
        bl = await load_blocklist(settings)
        assert "spam.test" in bl.blocked
        assert "scam.example" in bl.blocked
        assert "good.test" in bl.allowed

    async def test_load_from_file(self, tmp_path: pytest.Path) -> None:  # type: ignore[name-defined]
        bl_file = tmp_path / "bl.txt"
        bl_file.write_text("0.0.0.0 spam.test\nscam.example\n")
        settings = Settings(blocklist_file=bl_file)
        bl = await load_blocklist(settings)
        assert "spam.test" in bl.blocked
        assert "scam.example" in bl.blocked

    @respx.mock
    async def test_load_from_url(self) -> None:
        respx.get("https://example.com/list").mock(
            return_value=httpx.Response(200, text="bad.test\nworse.test\n")
        )
        settings = Settings(blocklist_urls="https://example.com/list")
        bl = await load_blocklist(settings)
        assert "bad.test" in bl.blocked
        assert "worse.test" in bl.blocked

    @respx.mock
    async def test_url_fetch_error_doesnt_crash(self) -> None:
        respx.get("https://example.com/list").mock(
            return_value=httpx.Response(500)
        )
        settings = Settings(blocklist_urls="https://example.com/list")
        bl = await load_blocklist(settings)
        assert bl.blocked == set()  # failed silently, empty set


    async def test_file_oserror_logged_not_raised(
        self, tmp_path: object,
    ) -> None:
        """A file that exists but is unreadable (permissions, broken
        symlink, etc.) must NOT crash load_blocklist — log + continue."""
        # Point at a real, readable file so file_path.exists() is True,
        # but mock read_text to raise OSError mid-load.
        from pathlib import Path
        from unittest.mock import patch
        f = Path(tmp_path) / "list.txt"  # type: ignore[arg-type]
        f.write_text("example.com\n")
        settings = Settings(blocklist_file=f)
        with patch.object(
            Path, "read_text",
            side_effect=PermissionError("denied"),
        ):
            bl = await load_blocklist(settings)
        assert bl.blocked == set()  # error swallowed → empty result
