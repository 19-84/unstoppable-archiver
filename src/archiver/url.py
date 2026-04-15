# ABOUTME: URL normalization and SHA-256 hashing for dedup level 1
# ABOUTME: Used by repository.create() to compute url_hash before storage
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false
"""URL normalization and hashing for deduplication."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import structlog
from beartype import beartype
from icontract import ensure, require

log = structlog.get_logger()

# Tracking parameters to strip before hashing
_DEFAULT_HTTP_PORT = 80
_DEFAULT_HTTPS_PORT = 443
_WWW_PREFIX_LEN = 4

TRACKING_PARAMS: frozenset[str] = frozenset({
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "fbclid",
    "gclid",
    "gclsrc",
    "dclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "twclid",
    "igshid",
    # "ref" deliberately excluded — used legitimately by many sites (Amazon, etc.)
    "_ga",
    "_gl",
    "yclid",
    "zanpid",
    "spm",
    "scm",
})


@beartype
@require(lambda url: len(url) > 0, "URL must not be empty")
@ensure(lambda result: "://" in result, "Result must contain scheme")
@ensure(lambda result: "#" not in result, "Result must not contain fragment")
def normalize_url(url: str, *, strip_www: bool = True) -> str:
    """Normalize a URL for consistent hashing.

    1. Lowercase scheme and host
    2. Remove default ports (:80, :443)
    3. Remove tracking parameters
    4. Sort remaining query parameters alphabetically
    5. Remove fragment
    6. Remove trailing slash on path
    7. Optionally strip www prefix
    """
    parsed = urlparse(url)

    # Lowercase scheme and host
    scheme = parsed.scheme.lower()
    host = parsed.hostname or ""
    host = host.lower()

    # Remove default ports
    port = parsed.port
    if (scheme == "http" and port == _DEFAULT_HTTP_PORT) or (
        scheme == "https" and port == _DEFAULT_HTTPS_PORT
    ):
        port = None

    netloc = host
    if strip_www and netloc.startswith("www."):
        netloc = netloc[_WWW_PREFIX_LEN:]
    if port is not None:
        netloc = f"{netloc}:{port}"
    # Strip credentials from URLs to avoid leaking them in logs/DB/WARC
    if parsed.username:
        log.warning("url.credentials_stripped", host=host)

    # Normalize path: remove trailing slash (but keep root /)
    path = parsed.path
    while len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if not path:
        path = "/"

    # Remove tracking params and sort remaining
    query_params = parse_qs(parsed.query, keep_blank_values=True)
    filtered = {
        k: v for k, v in query_params.items() if k.lower() not in TRACKING_PARAMS
    }
    sorted_query = urlencode(sorted(filtered.items()), doseq=True)

    # Strip fragment
    return urlunparse((scheme, netloc, path, parsed.params, sorted_query, ""))


@beartype
def url_hash(url: str) -> str:
    """Compute SHA-256 hash of a normalized URL."""
    normalized = normalize_url(url)
    return hashlib.sha256(normalized.encode()).hexdigest()
