# ABOUTME: Archive fallback capture for Tiers 4-5 (Wayback Machine, archive.today)
# ABOUTME: Checks availability via APIs, navigates to public archives, strips toolbars
"""Fallback capture from public web archives."""

from __future__ import annotations

import httpx
import structlog
from beartype import beartype
from playwright.async_api import Page

log = structlog.get_logger()

_WAYBACK_URL_PREFIX = "https://web.archive.org/web/2/"
_WAYBACK_API = "https://archive.org/wayback/available"
_ARCHIVE_TODAY_URL_PREFIX = "https://archive.today/newest/"

# Wayback Machine toolbar element IDs to strip
_WAYBACK_STRIP_SELECTORS = [
    "#wm-ib-bar",
    "#wm-ib",
    "#donato",
    "#wm-btm-bar",
    'script[src*="web.archive.org"]',
    'link[href*="web.archive.org"]',
]

# archive.today toolbar element IDs to strip
_ARCHIVE_TODAY_STRIP_SELECTORS = [
    "#HEADER",
    "#DIVSHARE",
    'script[src*="archive."]',
]


@beartype
async def check_wayback_availability(url: str) -> str | None:
    """Check if URL is archived in the Wayback Machine.

    Returns the snapshot URL if available, None otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _WAYBACK_API, params={"url": url}
            )
            if resp.status_code != 200:  # noqa: PLR2004
                return None
            data = resp.json()
            snapshot = data.get("archived_snapshots", {}).get("closest")
            if snapshot and snapshot.get("available"):
                return str(snapshot["url"])
    except Exception:
        log.debug("fallback.wayback.availability_check_failed", url=url)
    return None


async def capture_from_wayback(
    url: str,
    page: Page,
    timeout: int = 60000,
) -> bool:
    """Navigate to Wayback Machine and prepare page for capture.

    Returns True if a snapshot was found, False if not archived.
    The page is left in a state ready for SingleFile + screenshot capture
    with the Wayback toolbar stripped.
    """
    wayback_url = _WAYBACK_URL_PREFIX + url
    log.info("fallback.wayback.navigating", url=wayback_url)

    response = await page.goto(wayback_url, timeout=timeout)

    if response is None or response.status >= 400:  # noqa: PLR2004
        log.warning("fallback.wayback.not_found", url=url)
        return False

    # Check for "not archived" page
    title = await page.title()
    body = await page.evaluate(
        "document.body ? document.body.innerText : ''"
    )
    not_found_markers = [
        "Wayback Machine has not archived that URL",
        "The Wayback Machine has not archived",
        "This URL has been excluded",
        "Page cannot be crawled",
    ]
    for marker in not_found_markers:
        if marker in body or marker in title:
            log.warning(
                "fallback.wayback.not_archived", url=url
            )
            return False

    # Strip Wayback toolbar
    for selector in _WAYBACK_STRIP_SELECTORS:
        await page.evaluate(
            f"document.querySelectorAll('{selector}').forEach(e => e.remove())"
        )

    log.info("fallback.wayback.ready", url=url)
    return True


@beartype
async def check_archive_today_availability(url: str) -> bool:
    """Check if URL is likely archived on archive.today.

    Makes a HEAD request to archive.today/newest/URL. Returns True
    if a snapshot likely exists (non-redirect response).
    """
    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False
        ) as client:
            resp = await client.head(
                _ARCHIVE_TODAY_URL_PREFIX + url
            )
            # 200 = snapshot exists; 3xx redirect to homepage = no snapshot
            return resp.status_code == 200  # noqa: PLR2004
    except Exception:
        log.debug(
            "fallback.archive_today.availability_check_failed",
            url=url,
        )
    return False


async def capture_from_archive_today(
    url: str,
    page: Page,
    timeout: int = 90000,
) -> bool:
    """Navigate to archive.today and prepare page for capture.

    Returns True if a snapshot was found, False if not archived.
    archive.today uses aggressive CAPTCHAs — Camoufox stealth helps.
    """
    archive_url = _ARCHIVE_TODAY_URL_PREFIX + url
    log.info("fallback.archive_today.navigating", url=archive_url)

    response = await page.goto(archive_url, timeout=timeout)

    if response is None or response.status >= 400:  # noqa: PLR2004
        log.warning(
            "fallback.archive_today.not_found", url=url
        )
        return False

    # archive.today redirects to homepage if no snapshot exists
    current_url = page.url
    if current_url.rstrip("/") in (
        "https://archive.today",
        "https://archive.is",
        "https://archive.ph",
    ):
        log.warning(
            "fallback.archive_today.no_snapshot", url=url
        )
        return False

    # Strip archive.today toolbar
    for selector in _ARCHIVE_TODAY_STRIP_SELECTORS:
        await page.evaluate(
            f"document.querySelectorAll('{selector}').forEach(e => e.remove())"
        )

    log.info("fallback.archive_today.ready", url=url)
    return True
