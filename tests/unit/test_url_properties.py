# ABOUTME: Property-based tests for URL normalization via hypothesis
# ABOUTME: Verifies idempotency, determinism, and invariants of normalize_url and url_hash
"""Property-based tests for URL normalization."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from archiver.url import normalize_url, url_hash

pytestmark = pytest.mark.hypothesis

# Strategy: generate plausible HTTP(S) URLs
_url_strategy = st.from_regex(
    r"https?://[a-z]{1,10}\.[a-z]{2,4}(/[a-z0-9]{0,10}){0,3}(\?[a-z]=[a-z0-9]{1,5}(&[a-z]=[a-z0-9]{1,5}){0,3})?",
    fullmatch=True,
)


class TestNormalizeUrlProperties:
    @given(url=_url_strategy)
    @settings(max_examples=200)
    def test_idempotent(self, url: str) -> None:
        """normalize(normalize(x)) == normalize(x)."""
        once = normalize_url(url)
        twice = normalize_url(once)
        assert once == twice

    @given(url=_url_strategy)
    @settings(max_examples=200)
    def test_no_fragment_in_result(self, url: str) -> None:
        """Normalized URLs never contain fragments."""
        result = normalize_url(url)
        assert "#" not in result

    @given(url=_url_strategy)
    @settings(max_examples=200)
    def test_has_scheme(self, url: str) -> None:
        """Normalized URLs always contain a scheme."""
        result = normalize_url(url)
        assert "://" in result

    @given(url=_url_strategy)
    @settings(max_examples=200)
    def test_lowercase(self, url: str) -> None:
        """Scheme and host are always lowercase."""
        result = normalize_url(url)
        scheme, rest = result.split("://", 1)
        host = rest.split("/", 1)[0].split("?", 1)[0]
        assert scheme == scheme.lower()
        assert host == host.lower()


class TestUrlHashProperties:
    @given(url=_url_strategy)
    @settings(max_examples=200)
    def test_deterministic(self, url: str) -> None:
        """Same URL always produces the same hash."""
        assert url_hash(url) == url_hash(url)

    @given(url=_url_strategy)
    @settings(max_examples=200)
    def test_hex_format(self, url: str) -> None:
        """Hash is always a 64-char hex string."""
        h = url_hash(url)
        sha256_hex_len = 64
        assert len(h) == sha256_hex_len
        assert all(c in "0123456789abcdef" for c in h)
