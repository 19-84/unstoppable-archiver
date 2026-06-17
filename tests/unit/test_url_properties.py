# ABOUTME: Property-based tests for URL normalization via hypothesis
# ABOUTME: Verifies idempotency, determinism, and invariants of normalize_url and url_hash
"""Property-based tests for URL normalization."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from archiver.url import normalize_url, url_hash

pytestmark = pytest.mark.hypothesis

# Strategy: generate plausible HTTP(S) URLs. Composed from cheap text
# strategies rather than st.from_regex — a fullmatch regex with nested
# quantifiers is entropy-heavy and slow to draw, which tripped
# Hypothesis's wall-clock `too_slow` health check intermittently on
# loaded CI hosts. This builds the same shape (scheme://label.tld with
# up to 3 path segments and an optional &-joined query) far more cheaply.
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ALNUM = _LOWER + "0123456789"
_label = st.text(_LOWER, min_size=1, max_size=10)
_tld = st.text(_LOWER, min_size=2, max_size=4)
_segments = st.lists(st.text(_ALNUM, max_size=10), max_size=3).map(
    lambda segs: "".join("/" + s for s in segs)
)
_query = st.lists(
    st.tuples(
        st.text(_LOWER, min_size=1, max_size=1),
        st.text(_ALNUM, min_size=1, max_size=5),
    ),
    max_size=4,
).map(lambda ps: ("?" + "&".join(f"{k}={v}" for k, v in ps)) if ps else "")


@st.composite
def _urls(draw: st.DrawFn) -> str:
    scheme = draw(st.sampled_from(("http", "https")))
    return f"{scheme}://{draw(_label)}.{draw(_tld)}{draw(_segments)}{draw(_query)}"


_url_strategy = _urls()


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
