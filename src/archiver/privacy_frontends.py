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

import asyncio
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import structlog
from beartype import beartype

from archiver.url import apex_of

log = structlog.get_logger()


@dataclass(frozen=True)
class FrontendPolicy:
    """Routing policy for one target apex.

    `probe_path` + `probe_marker` drive content-positive validation:
    we navigate an instance to `{instance}{probe_path}` and assert that
    `probe_marker` is present in the rendered body. A 200 OK from an
    Anubis/CF challenge page otherwise looks identical to a 200 OK
    from real content — the marker is what discriminates.

    Pick `probe_path` to point at stable, long-lived content (a
    historical tweet, a classic article, a permanent subreddit) so
    probes don't break when upstream sites rotate their content.

    `not_found_marker` (optional) catches the case where the probe
    passes (instance is up, serves its homepage fine) but the
    instance doesn't have a specific user-requested URL cached.
    Scribe's "This article is missing" page is a ~120KB 200 OK that
    sneaks past the probe; the negative marker explicitly recognizes
    the per-URL not-found state. Empty string disables the check.
    """

    target_apex: str             # the site we're fronting (e.g. "reddit.com")
    instances: tuple[str, ...]   # base URLs to try in order (scheme://host)
    probe_path: str              # path for the canonical content probe
    probe_marker: str            # substring that must appear in real content
    not_found_marker: str = ""   # substring that flags per-URL absence


# Registry of (target_apex → policy). Order within `instances` is
# preference-order — the worker tries the first, falls through to the
# next on failure. All instances were empirically verified to accept
# traffic as of the last time this file was updated (2026-04-24); most
# return an Anubis PoW challenge on plain httpx but clear under
# Camoufox+SOCKS5.
FRONTENDS: tuple[FrontendPolicy, ...] = (
    # Medium paywall bypass. Scribe is the reference implementation;
    # LibMedium is a secondary hosted by batsense. We probe the
    # frontend's own homepage rather than a specific article — both
    # projects render articles on-demand from upstream Medium and
    # don't necessarily have any given article cached, so an article
    # probe was a flaky test of "is the instance up". Marker "Medium"
    # (capital M) matches both scribe ("frontend to Medium") and
    # libmedium ("LibMedium" branding); challenge/error shells don't
    # contain it.
    FrontendPolicy(
        target_apex="medium.com",
        instances=(
            "https://scribe.rip",
            "https://libmedium.batsense.net",
        ),
        probe_path="/",
        probe_marker="Medium",
        # Scribe returns its "This article is missing" page as a
        # 120KB 200 OK when an article isn't cached upstream — same
        # negative phrase appears across all article-not-found cases.
        not_found_marker="This article is missing",
    ),
    # Twitter / X. Nitter's content endpoints effectively returned
    # empty bodies since guest-account removal; xcancel is the one
    # working descendant as of 2026-Q2. Probe target is Jack's first
    # tweet (2006) — permanent.
    FrontendPolicy(
        target_apex="twitter.com",
        instances=("https://xcancel.com",),
        probe_path="/jack/status/20",
        probe_marker="just setting up my twttr",
    ),
    FrontendPolicy(
        target_apex="x.com",
        instances=("https://xcancel.com",),
        probe_path="/jack/status/20",
        probe_marker="just setting up my twttr",
    ),
    # Reddit. Redlib (fork of Libreddit) maintains an instance list
    # separately; clearnet instances we've confirmed TCP-reachable.
    # The top two don't CF-403 our server IP — they use Anubis which
    # Camoufox clears. The third is kept as a tertiary despite being
    # sometimes CF-walled (the gate-passing SOCKS5 clears CF too).
    # Probe target is r/announcements — Reddit's official channel,
    # never renamed, always public.
    FrontendPolicy(
        target_apex="reddit.com",
        instances=(
            "https://redlib.privacyredirect.com",
            "https://redlib.privadency.com",
            "https://redlib.perennialte.ch",
        ),
        probe_path="/r/announcements/",
        probe_marker="r/announcements",
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


@beartype
async def probe_frontend_instance(
    policy: FrontendPolicy,
    instance_base: str,
    proxy_server: str,
    timeout: float = 60.0,
) -> bool:
    """Does `instance_base` serve real content (not just an Anubis challenge)?

    Navigates through Camoufox + the given SOCKS5 proxy to the
    policy's canonical probe URL and waits up to `timeout` seconds
    for the `probe_marker` to appear in the page body. Challenge
    pages and "Welcome" shells lack the marker so they fail cleanly.

    Camoufox handles CF/Anubis JS resolution automatically given
    enough time; we poll rather than relying on `wait_for_function`
    so the same shape works across every frontend's challenge UI.
    Returns False on any exception — a dead instance is a failed
    probe, same bucket as a challenge-gated one.
    """
    from camoufox.async_api import (  # type: ignore[import-untyped]
        AsyncCamoufox,
    )

    probe_url = instance_base.rstrip("/") + policy.probe_path
    try:
        async with AsyncCamoufox(
            headless="virtual",
            humanize=True,
            geoip=False,
            proxy={"server": proxy_server},
        ) as browser:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            try:
                await page.goto(
                    probe_url,
                    wait_until="domcontentloaded",
                    timeout=int(timeout * 1000),
                )
                # Poll for the content marker. Give challenge resolvers
                # up to ~30 s total by checking every 3 s.
                for _ in range(10):
                    body = await page.content()
                    if policy.probe_marker in body:
                        log.debug(
                            "privacy_frontend.probe_passed",
                            instance=instance_base,
                            target=policy.target_apex,
                        )
                        return True
                    await asyncio.sleep(3)
                log.debug(
                    "privacy_frontend.probe_no_marker",
                    instance=instance_base,
                    target=policy.target_apex,
                )
                return False
            finally:
                await page.close()
                await context.close()
    except Exception as exc:
        log.debug(
            "privacy_frontend.probe_error",
            instance=instance_base,
            target=policy.target_apex,
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        return False
