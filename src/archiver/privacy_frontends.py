# ABOUTME: Per-apex privacy-frontend registry + URL rewrite for Tier 4 fallback
# ABOUTME: Returns a FrontendPolicy matching a URL's apex, or None for non-eligible URLs
"""Privacy-frontend registry and URL rewriting.

When our own stealth tiers (1-3) fail on a site we have a privacy
frontend for, the PRIVACY_FRONTEND tier rewrites the URL to a known
frontend instance and captures through it. The frontend typically
clears the target site's anti-bot measures by having its own scraping
infrastructure that the origin accepts.

Scoped intentionally small:
- Only apexes where our direct capture frequently fails AND a working
  frontend exists. IMDb/Fandom/StackOverflow already capture cleanly
  through Camoufox — no need to rewrite.
- Frontends themselves are near-universally Anubis/CF gated from a
  server IP. The worker routes through the same gate-passing SOCKS5
  pool we built for archive.today so Camoufox can clear challenges.

Custom-domain Medium (e.g. vgr.medium.com, or fully-custom like
levelup.gitconnected.com) is a larger detection problem and is
deliberately out of scope for v1 — plain ``medium.com/@author/slug``
URLs only.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from beartype import beartype

from archiver.url import apex_of


@dataclass(frozen=True)
class FrontendPolicy:
    """Routing policy for one target apex."""

    target_apex: str             # the site we're fronting (e.g. "reddit.com")
    instances: tuple[str, ...]   # base URLs to try in order (scheme://host)


# Registry of (target_apex → policy). Order within `instances` is
# preference-order — the worker tries the first, falls through to the
# next on failure. All instances were empirically verified to accept
# traffic as of the last time this file was updated (2026-04-24); most
# return an Anubis PoW challenge on plain httpx but clear under
# Camoufox+SOCKS5.
FRONTENDS: tuple[FrontendPolicy, ...] = (
    # Medium paywall bypass. Scribe is the reference implementation;
    # LibMedium is a secondary hosted by batsense.
    FrontendPolicy(
        target_apex="medium.com",
        instances=(
            "https://scribe.rip",
            "https://libmedium.batsense.net",
        ),
    ),
    # Twitter / X. Nitter's content endpoints effectively returned
    # empty bodies since guest-account removal; xcancel is the one
    # working descendant as of 2026-Q2.
    FrontendPolicy(
        target_apex="twitter.com",
        instances=("https://xcancel.com",),
    ),
    FrontendPolicy(
        target_apex="x.com",
        instances=("https://xcancel.com",),
    ),
    # Reddit. Redlib (fork of Libreddit) maintains an instance list
    # separately; clearnet instances we've confirmed TCP-reachable.
    # The top two don't CF-403 our server IP — they use Anubis which
    # Camoufox clears. The third is kept as a tertiary despite being
    # sometimes CF-walled (the gate-passing SOCKS5 clears CF too).
    FrontendPolicy(
        target_apex="reddit.com",
        instances=(
            "https://redlib.privacyredirect.com",
            "https://redlib.privadency.com",
            "https://redlib.perennialte.ch",
        ),
    ),
)


@beartype
def resolve_policy(url: str) -> FrontendPolicy | None:
    """Return the FrontendPolicy whose target_apex matches the URL, or None.

    Matches an apex exactly (``reddit.com``) or any subdomain of it
    (``old.reddit.com``, ``www.reddit.com``). Returns None for URLs
    that aren't registered — the worker then escalates to the next
    tier rather than running this one.
    """
    host = apex_of(url)
    if not host:
        return None
    for policy in FRONTENDS:
        if host == policy.target_apex or host.endswith(
            "." + policy.target_apex
        ):
            return policy
    return None


@beartype
def rewrite_to_instance(url: str, instance_base: str) -> str:
    """Swap a URL's scheme+netloc for `instance_base`, preserving path + query.

    Example:
        rewrite_to_instance(
            "https://www.reddit.com/r/tech/comments/abc/",
            "https://redlib.privacyredirect.com",
        )
        == "https://redlib.privacyredirect.com/r/tech/comments/abc/"
    """
    parsed = urlparse(url)
    base = urlparse(instance_base)
    return urlunparse(
        (
            base.scheme,
            base.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            "",  # drop fragment — never useful in a captured archive
        )
    )
