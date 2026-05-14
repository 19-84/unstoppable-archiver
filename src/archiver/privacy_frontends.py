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
import contextlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

import structlog
from beartype import beartype
from icontract import require

from archiver.url import apex_of

log = structlog.get_logger()

_MAX_PORT = 65535


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

    `not_found_markers` (optional) catches the case where the probe
    passes (instance is up, serves its homepage fine) but the
    instance returns a non-content shell for the user's specific URL:
    - scribe.rip serves "This article is missing" (200 OK, ~120KB)
      when an article isn't cached upstream
    - libmedium returns "502 Bad Gateway" (200 OK, ~300B) when its
      upstream Medium fetcher hiccups
    The marker tuple lets one policy enumerate every known shell so
    each instance's failure mode is recognized. Empty tuple disables
    the check entirely.

    `registry_url` + `registry_kind` (optional) point at the canonical
    upstream list of instances for this frontend family (e.g. the
    redlib-org/redlib-instances repo, or the d420.de status tracker).
    fetch_registry_instances() is called once per probe pass and its
    results are unioned with the hardcoded `instances` tuple, so new
    upstream mirrors get discovered + probed without a code change.
    `instances` stays as the durable fallback — registry fetch
    failures (offline, parse error) degrade silently to it.
    """

    target_apex: str                # site we're fronting (e.g. "reddit.com")
    instances: tuple[str, ...]      # base URLs to try in order (fallback)
    probe_path: str                 # path for canonical content probe
    probe_marker: str               # substring required in real content
    not_found_markers: tuple[str, ...] = ()  # substrings flagging absence
    registry_url: str | None = None   # upstream-curated instance list URL
    registry_kind: str | None = None  # parser key: 'redlib-json' or 'd420-html'


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
        # Each marker is specific enough that a legitimate Medium
        # article wouldn't contain it. "Welcome" is the scribe
        # homepage title (returned when the article slug 404s).
        not_found_markers=(
            "This article is missing",      # scribe 404 body
            "<title>Welcome</title>",       # scribe 404 / homepage shell
            "<title>502 Bad Gateway",       # libmedium upstream hiccup
            "<title>503 Service Unavailable", # libmedium upstream hiccup
            "<title>504 Gateway Timeout",   # libmedium upstream hiccup
        ),
    ),
    # Twitter / X. After Nitter's guest-account removal most upstream
    # instances served empty bodies; xcancel was the first descendant
    # to maintain content. The roster below mirrors the public uptime
    # tracker at status.d420.de — ten instances confirmed reachable as
    # of 2026-05-14, ordered by recent uptime (xcancel 97% first).
    # All ten are challenge-walled (Anubis or CF) from server IPs, so
    # the worker must reach them through Camoufox + the gate-passing
    # SOCKS5 pool. The content-positive probe ("just setting up my
    # twttr" on Jack's permanent 2006 tweet) discards instances that
    # only serve challenge shells or have lost upstream scraping.
    FrontendPolicy(
        target_apex="twitter.com",
        instances=(
            "https://xcancel.com",                 # d420 97% uptime
            "https://nitter.space",                # 96%
            "https://nuku.trabun.org",             # 95%
            "https://lightbrd.com",                # 95%
            "https://nitter.net",                  # 94% (origin)
            "https://nitter.privacyredirect.com",  # 91%
            "https://nitter.kareem.one",           # 89%
            "https://nitter.poast.org",            # 86%
            "https://nitter.catsarch.com",         # 68%
            "https://nitter.tiekoetter.com",       # 44% (kept; gate validates)
        ),
        probe_path="/jack/status/20",
        probe_marker="just setting up my twttr",
        registry_url="https://status.d420.de/",
        registry_kind="d420-html",
    ),
    FrontendPolicy(
        target_apex="x.com",
        instances=(
            "https://xcancel.com",
            "https://nitter.space",
            "https://nuku.trabun.org",
            "https://lightbrd.com",
            "https://nitter.net",
            "https://nitter.privacyredirect.com",
            "https://nitter.kareem.one",
            "https://nitter.poast.org",
            "https://nitter.catsarch.com",
            "https://nitter.tiekoetter.com",
        ),
        probe_path="/jack/status/20",
        probe_marker="just setting up my twttr",
        registry_url="https://status.d420.de/",
        registry_kind="d420-html",
    ),
    # Reddit. Roster mirrors the official redlib-org/redlib-instances
    # repo (instances.json) as of 2026-05-13 — seven clearnet mirrors;
    # the eighth is .onion (different code path, not included). Some
    # carry the cloudflare:true flag meaning the gate-passing SOCKS5
    # has to clear CF in addition to whatever the instance itself
    # serves; Camoufox handles both. Probe target is r/announcements
    # — Reddit's official channel, never renamed, always public.
    FrontendPolicy(
        target_apex="reddit.com",
        instances=(
            "https://redlib.catsarch.com",        # US
            "https://redlib.perennialte.ch",      # AU, CF
            "https://redlib.r4fo.com",            # DE, CF
            "https://red.artemislena.eu",         # DE
            "https://redlib.cow.rip",             # IN, CF
            "https://redlib.nadeko.net",          # CL
            "https://redlib.privadency.com",      # DE
        ),
        probe_path="/r/announcements/",
        probe_marker="r/announcements",
        registry_url=(
            "https://raw.githubusercontent.com/redlib-org/"
            "redlib-instances/main/instances.json"
        ),
        registry_kind="redlib-json",
    ),
)


@beartype
async def _fetch_redlib_json(url: str, timeout: float) -> tuple[str, ...]:
    """Pull the canonical clearnet redlib instance list.

    Schema (redlib-org/redlib-instances instances.json):
        {"updated": "YYYY-MM-DD",
         "instances": [{"url": "https://...", "country": "..", ...},
                       {"onion": "http://...onion", ...}]}

    Drops .onion entries (Tor-only — different code path) and
    anything missing a https URL.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # any failure -> empty fallback
        log.warning(
            "privacy_frontend.registry_fetch_failed",
            url=url,
            kind="redlib-json",
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        return ()

    out: list[str] = []
    for entry in data.get("instances", []):
        u = entry.get("url")
        if isinstance(u, str) and u.startswith("https://"):
            out.append(u.rstrip("/"))
    return tuple(out)


_D420_ROW_RE = re.compile(
    r'<a rel="nofollow external" href="(https://[^"/]+)"',
)


@beartype
async def _fetch_d420_html(url: str, timeout: float) -> tuple[str, ...]:
    """Pull active Nitter instances from status.d420.de.

    The page renders one <a rel="nofollow external" href="https://..."
    per instance in its main table. We extract every external link,
    drop the github/wikipedia ones, and de-dupe — same heuristic that
    cross-checked against the curated farside.link list cleanly.
    """
    import html as _html

    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            # d420 is bot-walled to non-browser UAs — pin a desktop UA
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) "
                    "Gecko/20100101 Firefox/130.0"
                ),
            },
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = _html.unescape(resp.text)
    except Exception as exc:
        log.warning(
            "privacy_frontend.registry_fetch_failed",
            url=url,
            kind="d420-html",
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        return ()

    seen: set[str] = set()
    out: list[str] = []
    for match in _D420_ROW_RE.finditer(text):
        host = match.group(1).rstrip("/")
        # Drop the meta-links that share rel="nofollow external" but
        # aren't instances (github, wikipedia, time.is, git.*)
        bare = host.replace("https://", "")
        if (
            "github.com" in bare
            or "wikipedia.org" in bare
            or "time.is" in bare
            or bare.startswith("git.")
        ):
            continue
        if host not in seen:
            seen.add(host)
            out.append(host)
    return tuple(out)


@beartype
async def fetch_registry_instances(
    policy: FrontendPolicy, timeout: float = 10.0,
) -> tuple[str, ...]:
    """Pull the current upstream-curated instance list for `policy`.

    Returns an empty tuple if the policy has no registry configured,
    or if the fetch / parse fails. Caller is expected to union the
    result with `policy.instances` (the durable fallback) so registry
    outages don't leave the worker with nothing to probe.

    Called once per probe pass (hourly by default) — well below any
    registry's rate limits.
    """
    if not policy.registry_url or not policy.registry_kind:
        return ()
    if policy.registry_kind == "redlib-json":
        return await _fetch_redlib_json(policy.registry_url, timeout)
    if policy.registry_kind == "d420-html":
        return await _fetch_d420_html(policy.registry_url, timeout)
    log.warning(
        "privacy_frontend.registry_kind_unknown",
        kind=policy.registry_kind,
        url=policy.registry_url,
    )
    return ()


@beartype
async def discover_instances(
    policy: FrontendPolicy, timeout: float = 10.0,
) -> tuple[str, ...]:
    """Union the static fallback with the live upstream registry.

    Order: static `instances` first (preference order preserved),
    then any registry-discovered entries not already present. Output
    is de-duped — the same instance can appear in both lists
    (frequently does) and we only want to probe each once.
    """
    upstream = await fetch_registry_instances(policy, timeout=timeout)
    seen: set[str] = set()
    out: list[str] = []
    for inst in policy.instances + upstream:
        if inst not in seen:
            seen.add(inst)
            out.append(inst)
    return tuple(out)


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


_HEAD_RE = re.compile(r"<head\b[^>]*>.*?</head>", re.I | re.S)


@beartype
def _strip_head(html: str) -> str:
    """Return the HTML with its <head>...</head> block removed.

    The probe's marker check must run against rendered body content,
    not server-side metadata. Nitter (and most fediverse frontends)
    pre-render Open Graph cards into <meta> tags so social-card
    unfurlers can preview challenge-walled pages — Anubis preserves
    the original <head> on its challenge wrapper, so a naive
    'marker in page.content()' silently passes the probe while the
    user is still staring at "Making sure you're not a bot".
    Stripping <head> forces the marker check to evaluate the actual
    visible content, which a still-challenged page does not have.
    """
    return _HEAD_RE.sub("", html, count=1)


@beartype
@require(lambda port: 1 <= port <= _MAX_PORT)
@require(lambda host: bool(host) and "/" not in host)
async def is_alive_tcp(host: str, port: int = 443, timeout: float = 4.0) -> bool:
    """Cheap reachability check: can we open a TLS connection to host:port?

    Fast pre-filter for the expensive Camoufox probe — a dead host
    (DNS NXDOMAIN, refused, unreachable) fails here in <1 s; the
    Camoufox probe would otherwise spin up a full browser for ~30 s
    before timing out on the same host. Does NOT validate that the
    server speaks HTTP or returns content — that's the antibot
    probe's job. A 403/503/200-challenge response all qualify as
    "alive"; only true network-layer failures fail.
    """
    try:
        fut = asyncio.open_connection(
            host, port, ssl=True, server_hostname=host,
        )
        _, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except (TimeoutError, OSError) as exc:
        log.debug(
            "privacy_frontend.alive_check_failed",
            host=host,
            port=port,
            error_type=type(exc).__name__,
        )
        return False


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
    for the `probe_marker` to appear in the page body **after**
    stripping <head>. Challenge pages and "Welcome" shells lack
    the marker in the visible body so they fail cleanly even when
    Nitter pre-renders OG metadata in <meta> tags.

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
                    body = _strip_head(await page.content())
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
