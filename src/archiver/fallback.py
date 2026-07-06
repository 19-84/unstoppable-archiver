# ABOUTME: Archive fallback capture for Tiers 4-5 (Wayback Machine, archive.today)
# ABOUTME: Availability lookup, URL normalization, Save Page Now, and submission flows
"""Fallback capture from public web archives."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse, urlunparse

import structlog
from beartype import beartype
from playwright.async_api import Page

from archiver.errors import FetchError
from archiver.http_client import fetch

log = structlog.get_logger()

# Timemaps and availability JSON are small; a response anywhere near
# these caps is the upstream misbehaving.
_AVAILABILITY_MAX_BYTES = 1 * 1024 * 1024
_TIMEMAP_MAX_BYTES = 4 * 1024 * 1024

_WAYBACK_API = "https://archive.org/wayback/available"
_WAYBACK_SPN_PREFIX = "https://web.archive.org/save/"
_ARCHIVE_TODAY_SUBMIT_URL = "https://archive.today/submit/"

# archive.today operates under multiple mirror hostnames that share the
# same backend but have independent CF routing and rate limits. Trying
# each in parallel materially improves hit rate when one is blocked.
# See https://archive.today/ (FAQ lists current mirrors).
ARCHIVE_TODAY_MIRRORS: tuple[str, ...] = (
    "archive.today",
    "archive.ph",
    "archive.is",
    "archive.li",
    "archive.fo",
    "archive.md",
    "archive.vn",
)

# Stealth headers for httpx direct-fetch against archive.today mirrors.
# The User-Agent is picked from a rotating pool of current real-world
# browser UAs (see archiver.user_agents) — never identifies this
# project. Call `_stealth_headers()` per request so each fetch picks
# a fresh UA.
from archiver import user_agents as _ua  # noqa: E402


def _stealth_headers() -> dict[str, str]:
    return {
        "User-Agent": _ua.pick(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }

# Wayback Machine toolbar element IDs to strip
WAYBACK_STRIP_SELECTORS: list[str] = [
    "#wm-ib-bar",
    "#wm-ib",
    "#donato",
    "#wm-btm-bar",
    'script[src*="web.archive.org"]',
    'link[href*="web.archive.org"]',
]

# archive.today toolbar element IDs to strip
ARCHIVE_TODAY_STRIP_SELECTORS: list[str] = [
    "#HEADER",
    "#DIVSHARE",
    'script[src*="archive."]',
]



def _wayback_url_variants(url: str) -> list[str]:
    """Generate URL variants to try when checking Wayback availability.

    Wayback's canonicalization is inconsistent — a user-submitted URL
    like `https://example.com/` may be archived under `example.com` (no
    trailing slash) or `www.example.com/`. We try the original first,
    then permute trailing slash and www prefix.
    """
    variants: list[str] = [url]
    try:
        p = urlparse(url)
        if not p.hostname:
            return variants
        path = p.path or "/"
        # Toggle trailing slash (only if path is non-root)
        if path == "/":
            variants.append(urlunparse(p._replace(path="")))
        elif path.endswith("/"):
            variants.append(
                urlunparse(p._replace(path=path.rstrip("/")))
            )
        else:
            variants.append(urlunparse(p._replace(path=path + "/")))
        # Toggle www. prefix
        if p.hostname.startswith("www."):
            alt_host = p.hostname[4:]
        else:
            alt_host = "www." + p.hostname
        port = f":{p.port}" if p.port else ""
        variants.append(
            urlunparse(p._replace(netloc=alt_host + port))
        )
    except Exception:
        log.debug("fallback.wayback.variant_generation_failed", url=url)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


@beartype
async def check_wayback_availability(url: str) -> str | None:
    """Check if URL is archived in the Wayback Machine.

    Tries URL variants (trailing slash, www prefix) before giving up
    because Wayback's canonicalization doesn't always match ours.
    Returns the snapshot URL if any variant hits, None otherwise.

    Transient failures and rate limits are retried with backoff inside
    `fetch`; two attempts per variant keeps the worst case (all
    variants down) bounded.
    """
    for variant in _wayback_url_variants(url):
        try:
            resp = await fetch(
                _WAYBACK_API,
                params={"url": variant},
                timeout=10.0,
                attempts=2,
                max_bytes=_AVAILABILITY_MAX_BYTES,
            )
        except FetchError:
            log.debug(
                "fallback.wayback.availability_check_failed",
                url=variant,
            )
            continue
        if resp.status_code != 200:  # noqa: PLR2004
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: S112
            # Malformed JSON from Wayback — try next variant.
            continue
        snapshot = data.get("archived_snapshots", {}).get("closest")
        if snapshot and snapshot.get("available"):
            log.info(
                "fallback.wayback.snapshot_found",
                variant=variant,
                snapshot=snapshot["url"],
            )
            return str(snapshot["url"])
    return None


@beartype
async def save_to_wayback(
    url: str, page: Page, timeout: int = 90000
) -> str | None:
    """Submit a URL to the Wayback Machine's Save Page Now endpoint.

    Navigates to https://web.archive.org/save/URL with a real browser.
    SPN renders a progress page and then redirects to the newly created
    snapshot URL. Typical latency is 30-60 s for cooperating origins;
    sites that block the Wayback crawler (NYT, WSJ, etc.) can hang
    indefinitely, so the default 90 s timeout gives up and escalates.

    Returns the snapshot URL on success, None on timeout/rate-limit/
    error. Rate-limited by Wayback to roughly 15 saves per minute per IP.
    """
    spn_url = _WAYBACK_SPN_PREFIX + url
    log.info("fallback.wayback.spn_navigating", url=spn_url)
    try:
        response = await page.goto(
            spn_url, timeout=timeout, wait_until="domcontentloaded"
        )
    except Exception as exc:
        log.warning(
            "fallback.wayback.spn_goto_failed", url=url, error=str(exc)
        )
        return None

    # If SPN itself returned an error (rate-limit, blocklist, maintenance),
    # don't even try to wait for a snapshot redirect.
    if response is not None and response.status >= 400:  # noqa: PLR2004
        log.warning(
            "fallback.wayback.spn_rejected",
            url=url,
            status=response.status,
            final_url=page.url,
        )
        return None

    # SPN bounces through a progress page, then lands on the snapshot
    # URL (/web/YYYYMMDDHHMMSS/<url>). Poll for final state.
    try:
        await page.wait_for_function(
            "() => location.pathname.match(/^\\/web\\/[0-9]{14}\\//)"
            " && !location.pathname.includes('/save/')",
            timeout=timeout,
        )
        final_url = page.url
        log.info("fallback.wayback.spn_saved", url=url, snapshot=final_url)
        return final_url
    except Exception:
        log.warning(
            "fallback.wayback.spn_timeout",
            url=url,
            final_url=page.url,
        )
        return None


@beartype
async def find_archive_today_snapshot(
    url: str, proxy: str | None = None
) -> str | None:
    """Return the newest archive.today snapshot URL for `url`, or None.

    Queries the timemap endpoint on each mirror in parallel and returns
    the first memento URL found. The timemap endpoint is served as
    text/plain outside the JS-gated UI — different CF routing, no
    challenge for most requests.

    Trying all mirrors in parallel means the lookup latency equals the
    slowest mirror (not the sum), and we succeed as soon as any mirror
    responds with a snapshot.

    `proxy`, if given, is a SOCKS5/HTTP URL routed through our
    gate-passing pool — the archive.today CF edge scores our direct
    server IP poorly so proxied reads recover coverage we can't get
    any other way.
    """
    async def _query(host: str) -> str | None:
        return await _timemap_latest_memento(host, url, proxy=proxy)

    tasks = [asyncio.create_task(_query(h)) for h in ARCHIVE_TODAY_MIRRORS]
    try:
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result:
                # Cancel remaining lookups; we have what we need.
                for t in tasks:
                    if not t.done():
                        t.cancel()
                log.info(
                    "fallback.archive_today.memento_found",
                    url=url,
                    memento=result,
                )
                return result
    finally:
        # Ensure every task is awaited/cancelled so we don't leak.
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return None


async def _timemap_latest_memento(
    mirror_host: str, url: str, proxy: str | None = None
) -> str | None:
    """Fetch timemap for `url` on `mirror_host` and return newest memento.

    timemap entries look like:
      <https://archive.today/2024/http://example.com>; rel="memento";
         datetime="Mon, 01 Jan 2024 00:00:00 GMT",
    Returns the URL of the latest-datetime memento, or None.
    """
    try:
        # attempts=1: the parallel mirror fan-out in the caller IS the
        # retry strategy — re-hitting a rate-limited mirror with
        # backoff would just delay rotation to a healthy peer.
        resp = await fetch(
            f"https://{mirror_host}/timemap/{url}",
            timeout=12.0,
            follow_redirects=True,
            headers={**_stealth_headers(),
                     "Accept": "application/link-format, text/plain, */*"},
            proxy=proxy,
            attempts=1,
            max_bytes=_TIMEMAP_MAX_BYTES,
            guard_private_ips=True,
        )
    except Exception as exc:
        log.debug(
            "fallback.archive_today.timemap_error",
            mirror=mirror_host,
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        return None
    if resp.status_code != 200:  # noqa: PLR2004
        log.debug(
            "fallback.archive_today.timemap_bad_status",
            mirror=mirror_host,
            status=resp.status_code,
        )
        return None
    parsed = latest_memento_from_timemap(resp.text)
    return parsed[0] if parsed else None


@beartype
def latest_memento_from_timemap(
    body: str,
) -> tuple[str, datetime | None] | None:
    """Parse an RFC 7089 link-format timemap; return the newest memento.

    Shared by the archive.today tier and the federated Memento tier —
    every Memento-compliant archive serves this format. Returns
    ``(memento_url, memento_datetime)``; the datetime is None when the
    winning entry had no parseable ``datetime`` attribute. Returns None
    when the timemap has no ``rel="memento"`` entries at all.
    """
    if 'rel="memento"' not in body:
        return None

    # Split into individual link-format entries. A naive `split(",")`
    # corrupts RFC-822 datetimes like "Sat, 01 Jan 2022 ..." — the
    # entry separator is a comma-before-angle-bracket pattern at the
    # start of the next link value.
    entries = re.split(r",\s*(?=<)", body)

    # Parse each entry, pick the latest `rel="memento"` by datetime.
    # Naive string compare on RFC-822 breaks ("Mon" < "Sat" < "Sun"
    # lexicographically), so we parse to real datetimes.
    latest_url: str | None = None
    latest_dt: datetime | None = None
    for block in entries:
        if 'rel="memento"' not in block:
            continue
        memento_url = _extract_angle_url(block)
        memento_dt_str = _extract_attr(block, "datetime")
        if not memento_url:
            continue
        try:
            memento_dt = parsedate_to_datetime(memento_dt_str)
        except (TypeError, ValueError):
            # Unparseable datetime — keep the entry as a fallback if
            # nothing else has landed yet.
            if latest_url is None:
                latest_url = memento_url
            continue
        if latest_dt is None or memento_dt >= latest_dt:
            latest_dt = memento_dt
            latest_url = memento_url
    if latest_url is None:
        return None
    return (latest_url, latest_dt)


def _extract_angle_url(block: str) -> str | None:
    """Return the URL enclosed in angle brackets, or None."""
    start = block.find("<")
    end = block.find(">", start + 1)
    if start == -1 or end == -1:
        return None
    return block[start + 1 : end].strip()


def _extract_attr(block: str, name: str) -> str:
    """Return the first `name="..."` attribute value in `block`, or ''."""
    needle = f'{name}="'
    start = block.find(needle)
    if start == -1:
        return ""
    start += len(needle)
    end = block.find('"', start)
    if end == -1:
        return ""
    return block[start:end]


# 14-digit YYYYMMDDHHMMSS runs, as used by Wayback, archive.today, and
# CDX indexes. Lookarounds keep a 15+-digit number from half-matching.
_SNAPSHOT_TS_RE = re.compile(r"(?<!\d)(\d{14})(?!\d)")


@beartype
def parse_snapshot_timestamp(value: str) -> datetime | None:
    """Parse a 14-digit YYYYMMDDHHMMSS archive timestamp to aware UTC.

    The format is shared by Wayback paths, archive.today mementos, and
    Common Crawl CDX records. Returns None on anything malformed.
    """
    # strptime is lenient (matches 1-digit %S etc.), so a 13-digit
    # string would silently parse to the wrong time — require exactly
    # the 14-digit form up front.
    if not _SNAPSHOT_TS_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None


@beartype
def memento_timestamp_from_url(url: str) -> datetime | None:
    """Extract the snapshot datetime embedded in a memento URL path.

    Handles ``web.archive.org/web/20240101123045/...`` and
    ``archive.today/20240101123045/...`` forms. Returns None when the
    path has no timestamp (e.g. archive.today short-id URLs).
    """
    try:
        path = urlparse(url).path
    except Exception:
        return None
    match = _SNAPSHOT_TS_RE.search(path)
    if not match:
        return None
    return parse_snapshot_timestamp(match.group(1))


def extract_title_from_html(html: str) -> str:
    """Cheap <title>…</title> extractor for direct-fetch results."""
    lower = html.lower()
    start = lower.find("<title")
    if start < 0:
        return ""
    start = lower.find(">", start) + 1
    end = lower.find("</title>", start)
    if end < 0:
        return ""
    return html[start:end].strip()[:500]


def strip_html_tags(html: str) -> str:
    """Very-rough tag stripping for search-index text extraction."""
    import re
    # Browsers close on end tags with trailing whitespace/attributes
    # (</script >), so the strict </script> form leaves script bodies
    # in the extracted text.
    text = re.sub(r"<script[^>]*>[\s\S]*?</script\b[^>]*>", " ", html, flags=re.I)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:50_000]


@beartype
async def fetch_archive_today_snapshot_html(
    memento_url: str, timeout: float = 20.0, proxy: str | None = None
) -> str | None:
    """Fetch a memento's raw HTML via httpx (bypasses browser + CF).

    archive.today's snapshot URLs (e.g. archive.today/2024/abc) are
    static, cached at the CF edge, and usually serve to a real-looking
    UA without challenge.

    When the primary mirror 4xx's, we also try the same snapshot path
    on every other mirror. archive.today uses a shared backend so the
    snapshot ID is globally valid; only the CF edge is mirror-specific,
    which means a mirror that's rate-limiting us now may have a peer
    mirror that isn't.
    """
    for candidate in _rotate_memento_across_mirrors(memento_url):
        result = await _try_fetch(candidate, timeout, proxy=proxy)
        if result is not None:
            return result
    return None


async def _try_fetch(
    url: str, timeout: float, proxy: str | None = None
) -> str | None:
    """One fetch attempt; returns HTML on success, None on any failure."""
    try:
        # attempts=1: mirror rotation in the caller handles rate limits
        # and transient failures better than same-mirror retries.
        resp = await fetch(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers=_stealth_headers(),
            proxy=proxy,
            attempts=1,
            guard_private_ips=True,
        )
    except Exception as exc:
        log.warning(
            "fallback.archive_today.direct_fetch_error",
            url=url,
            error=str(exc),
        )
        return None
    if resp.status_code != 200:  # noqa: PLR2004
        log.warning(
            "fallback.archive_today.direct_fetch_bad_status",
            url=url,
            status=resp.status_code,
        )
        return None
    lowered = resp.text[:4096].lower()
    if "just a moment" in lowered or "cf-browser-verification" in lowered:
        log.warning(
            "fallback.archive_today.direct_fetch_cf_challenge",
            url=url,
        )
        return None
    return resp.text


def _rotate_memento_across_mirrors(memento_url: str) -> list[str]:
    """Return memento_url rewritten for each known mirror.

    Given `http://archive.md/20240101/https://example.com`, yields
    ('http://archive.md/20240101/https://example.com',
     'http://archive.today/20240101/https://example.com',
     'http://archive.ph/20240101/https://example.com', ...).

    The first entry is the original URL (preserving its scheme and
    any non-mirror hostnames unchanged, to avoid rewriting incorrectly
    formed URLs).
    """
    out: list[str] = [memento_url]
    try:
        p = urlparse(memento_url)
    except Exception:
        return out
    host = (p.hostname or "").lower()
    # Only rotate when the hostname is one of our known mirrors.
    if host not in ARCHIVE_TODAY_MIRRORS:
        return out
    port = f":{p.port}" if p.port else ""
    for alt_host in ARCHIVE_TODAY_MIRRORS:
        if alt_host == host:
            continue
        candidate = urlunparse(p._replace(netloc=alt_host + port))
        out.append(candidate)
    return out


@beartype
async def save_to_archive_today(
    url: str, page: Page, timeout: int = 180000
) -> str | None:
    """Submit a URL to archive.today and return the resulting snapshot URL.

    Navigates to the archive.today homepage, waits for the submission
    form to appear (CF interstitial may gate it for 10-30 s), fills
    the URL, and waits for the WIP→snapshot redirect chain.

    archive.today sits behind Cloudflare + its own anti-bot layer so
    this MUST run on a stealth browser (Camoufox). Returns the snapshot
    URL on success, or None on any failure. Submissions take 30-120 s.
    """
    log.info("fallback.archive_today.submit_navigating", url=url)
    try:
        await page.goto(
            "https://archive.today/",
            timeout=timeout,
            wait_until="domcontentloaded",
        )
    except Exception as exc:
        log.warning(
            "fallback.archive_today.submit_goto_failed",
            url=url,
            error=str(exc),
        )
        return None

    # Wait for the submission form to become visible — archive.today
    # often shows a Cloudflare interstitial for 10-30s before the real
    # page appears. Using wait_for on the locator is more lenient than
    # page.fill's implicit wait.
    form_wait = min(timeout, 60000)
    try:
        await page.locator('input[name="url"]').first.wait_for(
            state="visible", timeout=form_wait
        )
    except Exception as exc:
        log.warning(
            "fallback.archive_today.form_not_found",
            url=url,
            error=str(exc),
        )
        return None

    # Fill and submit.
    try:
        await page.fill('input[name="url"]', url, timeout=15000)
        await page.click(
            'input[type="submit"], button[type="submit"]',
            timeout=15000,
        )
    except Exception as exc:
        log.warning(
            "fallback.archive_today.submit_form_failed",
            url=url,
            error=str(exc),
        )
        return None

    # After submit, archive.today either returns immediately with a
    # cached snapshot URL or bounces through a "work in progress" page
    # until the capture completes. Both terminate at /<shortid>/<url>.
    try:
        await page.wait_for_url(
            lambda current: _is_archive_today_snapshot_url(current),
            timeout=timeout,
        )
    except Exception:
        log.warning(
            "fallback.archive_today.submit_timeout",
            url=url,
            final_url=page.url,
        )
        return None

    snapshot_url = page.url
    log.info(
        "fallback.archive_today.submit_saved",
        url=url,
        snapshot=snapshot_url,
    )
    return snapshot_url


def _is_archive_today_snapshot_url(url: str) -> bool:
    """True if `url` looks like a final archive.today snapshot URL.

    Snapshot URLs have the form https://archive.today/<shortid> or
    https://archive.today/<shortid>/<original-url> and never contain
    /submit/ or /wip/ path segments.
    """
    try:
        p = urlparse(url)
    except Exception:
        return False
    if not p.hostname:
        return False
    if not any(
        p.hostname.endswith(suffix)
        for suffix in ("archive.today", "archive.is", "archive.ph")
    ):
        return False
    if any(seg in p.path for seg in ("/submit", "/wip/")):
        return False
    # Homepage — submission hasn't redirected yet
    path = p.path.rstrip("/")
    return bool(path)


