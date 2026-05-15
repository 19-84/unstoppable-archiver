#!/usr/bin/env python3
# ABOUTME: Internal-link validator for the running app (UAT smoke check)
# ABOUTME: Crawls every internal href from a seed page and asserts 2xx/3xx
"""Walk the app's internal links and report any 4xx/5xx.

Useful as a pre-deploy smoke check and as a regression sentinel after
template changes. NOT a security scanner — it doesn't try fuzz inputs,
auth bypasses, or anything destructive.

Default seed: the home page. The crawler:
  - Only follows same-origin links (won't hit example.com or external)
  - Skips query strings that would mutate (POST forms, /submit, etc)
  - Honours an explicit allowlist of safe routes
  - Reports anything that doesn't 2xx/3xx and exits non-zero

Run:
    docker compose exec -T app uv run python scripts/check_links.py \\
        http://app:8000

  or with a host-side app:
    .venv/bin/python scripts/check_links.py http://localhost:8000
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import deque
from urllib.parse import urljoin, urlparse

import httpx

# Routes we never follow even though we may extract them:
#  - /submit / /report / /recapture: state-changing POSTs
#  - /api/archives/*/delete: admin DELETE
#  - external links (filtered separately by netloc check)
#  - the bookmarklet javascript: href on the home page
_SKIP_PREFIXES = (
    "javascript:",
    "mailto:",
    "tel:",
)
_SKIP_PATH_PATTERNS = (
    re.compile(r"^/submit$"),
    re.compile(r"^/report/"),
    re.compile(r"^/recapture/"),
    re.compile(r"^/admin/logout$"),
    # Admin state-changing POST/DELETE endpoints — we'd flip moderation
    # state by hitting these. Auth'd crawl still walks the GET-side of
    # admin (dashboard, reports list, archive admin views).
    re.compile(r"^/admin/archives/[^/]+/hard-delete$"),
    re.compile(r"^/admin/archives/[^/]+/restore$"),
    re.compile(r"^/admin/archives/[^/]+/takedown$"),
    re.compile(r"^/admin/reports/[^/]+/resolve$"),
    re.compile(r"^/api/archives/.*/(snapshot|warc|screenshot|thumbnail)$"),
)


def _should_skip(href: str) -> bool:
    if href.startswith(_SKIP_PREFIXES):
        return True
    path = urlparse(href).path
    return any(p.match(path) for p in _SKIP_PATH_PATTERNS)


_HREF_RE = re.compile(r'href="([^"#?]+)(?:[#?][^"]*)?"')


def _extract_hrefs(html: str) -> set[str]:
    return set(_HREF_RE.findall(html))


def crawl(
    base: str,
    max_pages: int = 100,
    session_cookie: str | None = None,
) -> tuple[set[str], list[str]]:
    """Return (visited, errors). visited is every URL we GET'd cleanly;
    errors is every URL that returned >=400.

    Pass `session_cookie` (the literal value of the `session` cookie
    Starlette's SessionMiddleware sets after `/admin/login`) to crawl
    auth'd admin routes too. Without it, admin routes are still
    reachable but return their unauth response (redirect to login,
    which we follow, then 200).
    """
    base_host = urlparse(base).netloc
    visited: set[str] = set()
    queue: deque[str] = deque([base])
    errors: list[str] = []

    cookies = {"session": session_cookie} if session_cookie else None
    with httpx.Client(
        follow_redirects=True, timeout=10.0, cookies=cookies,
    ) as client:
        while queue and len(visited) < max_pages:
            url = queue.popleft()
            if url in visited:
                continue
            visited.add(url)

            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc}")
                continue

            if resp.status_code >= 400:  # noqa: PLR2004
                errors.append(f"{url}: HTTP {resp.status_code}")
                continue

            # Only crawl HTML responses for further links
            ctype = resp.headers.get("content-type", "")
            if "text/html" not in ctype:
                continue

            for href in _extract_hrefs(resp.text):
                if _should_skip(href):
                    continue
                absolute = urljoin(url, href)
                if urlparse(absolute).netloc != base_host:
                    continue
                if absolute not in visited:
                    queue.append(absolute)

    return visited, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "base", help="Base URL, e.g. http://localhost:8000",
    )
    parser.add_argument(
        "--max-pages", type=int, default=100,
        help="Hard cap on pages crawled (default: 100)",
    )
    parser.add_argument(
        "--cookie",
        help=(
            "Value of the `session` cookie from /admin/login. Lets the"
            " crawler walk auth'd admin routes. Get it from your"
            " browser DevTools after logging in."
        ),
    )
    args = parser.parse_args()

    base = args.base.rstrip("/")
    print(f"Crawling from {base}/")
    visited, errors = crawl(
        base, max_pages=args.max_pages, session_cookie=args.cookie,
    )
    print(f"Visited {len(visited)} pages")
    if errors:
        print(f"\n{len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print("All links healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
