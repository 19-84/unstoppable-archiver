# ABOUTME: HTML page routes for the Glass Noir frontend
# ABOUTME: Serves Jinja2 templates with htmx progressive enhancement
"""HTML page routes — server-rendered with htmx enhancement."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from archiver.blocklist import DomainBlocklist
from archiver.deps import (
    get_blocklist,
    get_client_ip_hash,
    get_db,
    get_settings,
)
from archiver.enums import ArchiveStatus
from archiver.rate_limit import enforce_limit
from archiver.repository import ArchiveRepository, PgConnection
from archiver.url import url_hash

router = APIRouter(tags=["pages"])

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _wayback_url(archive: object) -> str:
    """Jinja filter: produce the `/web/{ts}/{url}` link for an archive."""
    created_at = getattr(archive, "created_at", None)
    url = getattr(archive, "url", None)
    if created_at is None or url is None:
        return ""
    ts = created_at.strftime("%Y%m%d%H%M%S")
    return f"/web/{ts}/{url}"


def _safe_href(url: object) -> str:
    """Jinja filter: scheme-whitelist for URLs rendered into href attrs.

    The submission API blocks javascript:/file:/data: at intake, but
    archive.url is rendered as ``<a href="{{ archive.url }}">`` in
    multiple places, and a bypass (admin DB seed, migration, ingest
    job) would let a dangerous scheme execute as JS on click.
    Defense-in-depth: only http(s) URLs are rendered as-is; anything
    else returns ``#`` so the link is inert.

    The user-visible link TEXT is unaffected (still escaped by
    Jinja's autoescape); only the href value is sanitized."""
    if not isinstance(url, str):
        return "#"
    if url.startswith(("http://", "https://", "/")):
        return url
    return "#"


templates.env.filters["wayback_url"] = _wayback_url
templates.env.filters["safe_href"] = _safe_href

_archive_repo = ArchiveRepository()


# robots.txt: by default disallow crawler indexing of admin + API
# endpoints (the captured snapshots and search results are fair game
# for indexing but the operational surface isn't). The Sitemap line
# is computed per request so the URL is absolute (the protocol requires
# absolute URLs in robots.txt sitemap references).
_ROBOTS_BODY_PREFIX = (
    "User-agent: *\n"
    "Disallow: /admin/\n"
    "Disallow: /api/\n"
    "Disallow: /partials/\n"
    "Allow: /\n"
)


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots(request: Request) -> PlainTextResponse:
    settings = get_settings(request)
    body = _ROBOTS_BODY_PREFIX
    # In public mode there's no browsable index, so no sitemap to point at.
    if settings.mode == "self-hosted":
        body += f"\nSitemap: {request.base_url}sitemap.xml\n"
    return PlainTextResponse(
        body,
        headers={"Cache-Control": "public, max-age=86400"},
    )


# Sitemap protocol cap: 50,000 URLs per file. Most archives will sit
# well under this for a long time; once exceeded, the cleanest path
# is splitting into a sitemap index — left for later.
_SITEMAP_MAX_URLS = 50_000


@router.get("/sitemap.xml", response_model=None)
async def sitemap_xml(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> Response:
    """Search-engine sitemap of every public archive detail page.

    Without a sitemap, crawlers can only discover archives through
    the home recent-list (10 entries), the /archives browse paging,
    and search-result pages. That's a long crawl for a static
    catalogue. The sitemap exposes every non-removed archive's
    /archive/{id} URL with the last-modified time so search engines
    can incrementally re-index only what changed.

    404s in public mode to match the /archives discrimination — a
    public-facing instance shouldn't expose a complete index of
    submissions.
    """
    settings = get_settings(request)
    if settings.mode != "self-hosted":
        raise HTTPException(status_code=404, detail="Not available")

    # Restrict to completed captures only. Pending / capturing /
    # failed rows have no snapshot to index — feeding their URLs to a
    # search engine wastes crawl budget on pages that render an empty
    # 'still capturing' state, and on the next recrawl the snapshot
    # may still not exist. The frontend keeps showing pending rows in
    # the recent list and /archives browse so users can poll status,
    # but the sitemap is for external indexing and should only point
    # at terminal-state snapshots.
    rows = await conn.fetch(
        "SELECT id, completed_at, created_at FROM archives"
        " WHERE removed_at IS NULL AND status = 'complete'"
        " ORDER BY coalesce(completed_at, created_at) DESC"
        " LIMIT $1",
        _SITEMAP_MAX_URLS,
    )
    base = str(request.base_url).rstrip("/")
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for r in rows:
        lastmod = r["completed_at"] or r["created_at"]
        lastmod_iso = lastmod.isoformat() if lastmod is not None else ""
        lines.append(
            f"<url><loc>{base}/archive/{r['id']}</loc>"
            f"<lastmod>{lastmod_iso}</lastmod></url>"
        )
    lines.append("</urlset>")
    return Response(
        content="\n".join(lines),
        media_type="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/favicon.ico")
async def favicon() -> Response:
    """Return 204 No Content for favicon requests.

    Stops the browser from logging a 404 in its console every page load
    while we don't ship a real .ico. Replace with a FileResponse to a
    real asset when one exists.
    """
    return Response(status_code=204)


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse:
    """Home page with archive/search input and stats."""
    # Gather anonymous stats (exclude soft-deleted archives)
    stats_row = await conn.fetchrow(
        "SELECT"
        " count(*) as total_pages,"
        " count(DISTINCT split_part(url, '/', 3)) as total_domains,"
        " coalesce(sum(coalesce(snapshot_size, 0) + coalesce(warc_size, 0)), 0) as total_bytes,"
        " count(*) FILTER (WHERE status = 'complete') as complete_count,"
        " count(*) FILTER (WHERE status = 'failed') as failed_count"
        " FROM archives WHERE removed_at IS NULL"
    )
    total = stats_row["total_pages"] if stats_row else 0
    complete = stats_row["complete_count"] if stats_row else 0
    failed = stats_row["failed_count"] if stats_row else 0
    # Success rate is over TERMINAL captures only (complete + failed).
    # Counting pending / capturing archives in the denominator drags
    # the rate down for in-flight work that hasn't failed — a URL
    # submitted ten seconds ago isn't a failure, so it shouldn't
    # depress the headline number.
    finished = complete + failed
    stats = {
        "total_pages": total,
        "total_domains": stats_row["total_domains"] if stats_row else 0,
        "storage_mb": round((stats_row["total_bytes"] if stats_row else 0) / 1048576, 1),
        "success_rate": round(complete / finished * 100, 1) if finished > 0 else 0,
    }

    settings = get_settings(request)
    # Recent archives list — self-hosted only (public mode hides this to
    # avoid providing a browsable index of all public submissions)
    recent_archives: list[object] = []
    if settings.mode == "self-hosted":
        archives, _ = await _archive_repo.list_recent(
            conn, limit=10, offset=0
        )
        recent_archives = list(archives)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "stats": stats,
            "recent_archives": recent_archives,
            "mode": settings.mode,
        },
    )


@router.get("/archives", response_model=None)
async def archives_browse(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    limit: int = 25,
    offset: int = 0,
) -> HTMLResponse:
    """Browse all archives with pagination.

    The home page only shows the 10 most recent archives — older
    captures were unreachable through the UI without knowing the ULID
    or guessing keywords. This route is the public 'view all' surface
    in self-hosted mode. Public mode 404s to match the home-page mode
    discrimination (a public-facing instance shouldn't expose a
    browsable index of every submission).
    """
    settings = get_settings(request)
    if settings.mode != "self-hosted":
        raise HTTPException(status_code=404, detail="Not available")

    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    archives, total = await _archive_repo.list_recent(
        conn, limit=limit, offset=offset,
    )
    return templates.TemplateResponse(
        request,
        "archives_list.html",
        {
            "archives": archives, "total": total,
            "limit": limit, "offset": offset,
        },
    )


@router.post("/submit", response_model=None)
async def submit_form(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    blocklist: Annotated[DomainBlocklist, Depends(get_blocklist)],
) -> HTMLResponse | RedirectResponse:
    """Handle native HTML form submission (no-JS fallback).

    Accepts form-encoded data, creates archive, redirects to detail.
    """
    settings = get_settings(request)
    enforce_limit(request, settings.rate_limit_submit_per_hour)
    from archiver.enums import CaptureTier
    from archiver.repository import JobRepository

    form = await request.form()
    url = str(form.get("url", "")).strip()
    is_htmx = request.headers.get("HX-Request") == "true"

    def _submit_error(msg: str) -> HTMLResponse:
        # For htmx requests, return a compact partial with status 200
        # so htmx swaps it into #result-area. Non-htmx fallback (raw
        # form POST from the bookmarklet without JS) gets the full
        # index page at 400 so the browser renders a usable error.
        if is_htmx:
            return templates.TemplateResponse(
                request,
                "partials/submit_error.html",
                {"error": msg},
            )
        return templates.TemplateResponse(
            request,
            "index.html",
            {"stats": {}, "error": msg},
            status_code=400,
        )

    if not url:
        return _submit_error("Please enter a URL")

    # Detect if input is a search query (no http/https scheme = search)
    if not url.startswith(("http://", "https://")):
        from urllib.parse import quote

        return RedirectResponse(
            url=f"/search?q={quote(url, safe='')}", status_code=303
        )

    from archiver.url_safety import check_url_safety

    safety_error = check_url_safety(url, blocklist=blocklist)
    if safety_error:
        return _submit_error(safety_error)

    # Dedup: send the user to the existing capture if they submitted a
    # URL we captured within the recapture interval. /api/archives
    # returns 409 + existing_id for this case; the HTML form path
    # previously just created a fresh pending archive, wasting worker
    # time and confusing users who'd just hit submit twice. The HTML
    # surface is friendlier — redirect them straight at the existing
    # archive instead of an error page.
    uhash = url_hash(url)
    recent = await _archive_repo.check_recent_capture(
        conn, uhash, settings.recapture_interval_seconds,
    )
    if recent is not None:
        return RedirectResponse(
            url=f"/archive/{recent.id}", status_code=303,
        )

    archive = await _archive_repo.create(
        conn, url, submitter_ip_hash=get_client_ip_hash(request)
    )
    job_repo = JobRepository()
    await job_repo.enqueue(conn, archive.id, CaptureTier.CHROMIUM)

    return RedirectResponse(
        url=f"/archive/{archive.id}", status_code=303
    )


@router.post("/recapture/{archive_id}", response_model=None)
async def recapture(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> RedirectResponse:
    """Queue a fresh capture of an existing archive's URL (self-hosted only)."""
    settings = get_settings(request)
    if settings.mode != "self-hosted":
        raise HTTPException(status_code=404)
    enforce_limit(request, settings.rate_limit_submit_per_hour)

    from archiver.enums import CaptureTier
    from archiver.repository import JobRepository

    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")

    new_archive = await _archive_repo.create(
        conn, archive.url, submitter_ip_hash=get_client_ip_hash(request)
    )
    job_repo = JobRepository()
    await job_repo.enqueue(conn, new_archive.id, CaptureTier.CHROMIUM)

    return RedirectResponse(
        url=f"/archive/{new_archive.id}", status_code=303
    )


@router.get("/archive/{archive_id}", response_class=HTMLResponse)
async def archive_detail(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse:
    """Archive detail page.

    Three outcomes:
      - row missing entirely    -> 404 ("Archive not found")
      - row exists but removed  -> 410 with a takedown notice page,
                                   reason + date if admin recorded one
      - row exists, not removed -> 200, normal render
    Using `include_removed=True` lets us distinguish 'never existed'
    from 'taken down' — a 404 alone confuses legitimate revisits.
    """
    archive = await _archive_repo.get_by_id(
        conn, archive_id, include_removed=True,
    )
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    if archive.removed_at is not None:
        return templates.TemplateResponse(
            request,
            "archive_removed.html",
            {"archive": archive},
            status_code=410,
        )

    # Get snapshot history for this URL (filter removed)
    history = [
        h for h in await _archive_repo.get_by_url_hash(conn, archive.url_hash)
        if h.removed_at is None
    ]

    settings = get_settings(request)
    return templates.TemplateResponse(
        request,
        "archive_detail.html",
        {"archive": archive, "history": history, "mode": settings.mode},
    )


@router.get("/archive/{archive_id}/view", response_model=None)
async def archive_view(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse | RedirectResponse:
    """Legacy viewer URL — redirects to the Wayback-style /web/ form.

    Kept as a 301 redirect so external links and bookmarks keep working
    while /web/{ts}/{url} becomes the canonical viewer URL. If the
    archive was taken down, redirect to the detail page so the user
    lands on the friendly takedown stub instead of a bare 404.
    """
    archive = await _archive_repo.get_by_id(
        conn, archive_id, include_removed=True,
    )
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    if archive.removed_at is not None:
        return RedirectResponse(
            url=f"/archive/{archive_id}", status_code=303,
        )
    if archive.status != ArchiveStatus.COMPLETE:
        raise HTTPException(status_code=404, detail="Archive not complete")
    if not archive.artifact_dir:
        raise HTTPException(status_code=404, detail="No artifacts")

    ts = archive.created_at.strftime("%Y%m%d%H%M%S") if archive.created_at else ""
    if ts:
        return RedirectResponse(
            url=f"/web/{ts}/{archive.url}", status_code=301
        )
    siblings_pos, siblings_count, siblings_newer, siblings_older = await _archive_repo.get_siblings_info(
        conn, archive.url_hash, archive.id,
    )
    return templates.TemplateResponse(
        request, "archive_view.html",
        {
            "archive": archive,
            "siblings_count": siblings_count,
            "siblings_position": siblings_pos,
            "siblings_newer_id": siblings_newer,
            "siblings_older_id": siblings_older,
        },
    )


# --- Wayback-style URL routing ---
# Pattern: /web/{timestamp|latest}/{full-url}
#   /web/20260418234032/https://example.com/page
#   /web/latest/https://example.com/page
# Mirrors web.archive.org's URL scheme — makes archives referenceable
# by original URL + date rather than opaque ULID, and lets external
# links follow the familiar Wayback format.


@router.get("/web/latest/{url:path}", response_model=None)
async def wayback_latest(
    url: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse | RedirectResponse:
    """Resolve to the newest complete archive for `url` and render viewer."""
    target = _normalize_path_url(url, request)
    uhash = url_hash(target)
    archive = await _archive_repo.get_latest_complete(conn, uhash)
    if archive is None or not archive.artifact_dir:
        raise HTTPException(status_code=404, detail="No snapshot for this URL")
    siblings_pos, siblings_count, siblings_newer, siblings_older = await _archive_repo.get_siblings_info(
        conn, uhash, archive.id,
    )
    return templates.TemplateResponse(
        request, "archive_view.html",
        {
            "archive": archive,
            "siblings_count": siblings_count,
            "siblings_position": siblings_pos,
            "siblings_newer_id": siblings_newer,
            "siblings_older_id": siblings_older,
        },
    )


@router.get("/web/{timestamp}/{url:path}", response_model=None)
async def wayback_timestamped(
    timestamp: str,
    url: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse | RedirectResponse:
    """Resolve to the archive closest in time to `timestamp` and render viewer.

    Accepts both 14-digit exact timestamps (YYYYMMDDHHMMSS) and
    shorter prefixes (YYYY, YYYYMM, YYYYMMDD) which get padded.
    """
    # Accept truncated timestamps like `2026` or `20260418` — pad with
    # the latest-possible values so "give me the 2026 snapshot" picks
    # the newest one in that year.
    padded = _pad_timestamp(timestamp)
    if padded is None:
        raise HTTPException(status_code=400, detail="Invalid timestamp")
    target = _normalize_path_url(url, request)
    uhash = url_hash(target)
    archive = await _archive_repo.get_closest_to_timestamp(
        conn, uhash, padded
    )
    if archive is None or not archive.artifact_dir:
        raise HTTPException(status_code=404, detail="No snapshot near this timestamp")
    siblings_pos, siblings_count, siblings_newer, siblings_older = await _archive_repo.get_siblings_info(
        conn, uhash, archive.id,
    )
    return templates.TemplateResponse(
        request, "archive_view.html",
        {
            "archive": archive,
            "siblings_count": siblings_count,
            "siblings_position": siblings_pos,
            "siblings_newer_id": siblings_newer,
            "siblings_older_id": siblings_older,
        },
    )


def _normalize_path_url(raw: str, request: Request) -> str:
    """Normalize a URL parsed from a path segment.

    FastAPI's `{url:path}` strips a single leading slash after matching,
    and Starlette decodes `%2F` → `/` etc. We also preserve any query
    string the client sent (not part of the path match).
    """
    # Reconstruct the query string, if any.
    query = request.url.query
    if query:
        raw = f"{raw}?{query}"
    # Clients sometimes send `https:/example.com` (single slash after
    # scheme) because the path matcher collapses `//`. Repair that.
    for scheme in ("http", "https"):
        prefix = f"{scheme}:"
        if raw.startswith(prefix) and not raw.startswith(f"{scheme}://"):
            raw = f"{scheme}://{raw[len(prefix):].lstrip('/')}"
    return raw


def _pad_timestamp(ts: str) -> str | None:
    """Pad a partial YYYY[MM[DD[HH[MM[SS]]]]] to 14 digits.

    Shorter prefixes resolve to "the end of that period" so asking for
    `2026` returns the newest 2026 snapshot, not the first. Returns
    None if the input isn't a valid numeric prefix of 14 digits.
    """
    if not ts.isdigit() or not 4 <= len(ts) <= 14:  # noqa: PLR2004
        return None
    # Pad with the last moment of each field: month→12, day→31, time→59.
    pads = ["12", "31", "23", "59", "59"]
    out = ts
    # Positions: 4 (year done), 6 (month), 8 (day), 10 (hour), 12 (min), 14 (sec)
    while len(out) < 14:  # noqa: PLR2004
        out += pads[(len(out) - 4) // 2]
    return out[:14]


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    q: str = "",
    limit: int = 20,
    offset: int = 0,
) -> HTMLResponse:
    """Search results page."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    results = None
    if q.strip():
        results = await _archive_repo.search(
            conn, q, limit=limit, offset=offset
        )
    return templates.TemplateResponse(
        request,
        "search.html",
        {"query": q, "results": results, "limit": limit, "offset": offset},
    )


# --- htmx partials ---


@router.get("/partials/status/{archive_id}", response_class=HTMLResponse)
async def partial_status(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse:
    """Status badge partial for htmx polling.

    When the archive reaches a terminal state (complete or failed),
    return an HX-Refresh header so htmx reloads the whole detail
    page. Without this, a user watching a capture finish would see
    only the small status badge flip to 'Complete' — the main
    content area would stay frozen on the in-progress tier display,
    with no snapshot preview or download buttons, until they
    manually refreshed. The reload swaps in the proper completed /
    failed branch.
    """
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        return HTMLResponse('<span class="text-xs text-neutral-400">Not found</span>')
    response = templates.TemplateResponse(
        request,
        "partials/archive_status.html",
        {"archive": archive},
    )
    if archive.status in (ArchiveStatus.COMPLETE, ArchiveStatus.FAILED):
        # htmx sees this header and does a full-page GET, so the
        # detail page re-renders with the terminal-state branch.
        response.headers["HX-Refresh"] = "true"
    return response


@router.get("/partials/search", response_class=HTMLResponse)
async def partial_search(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    q: str = "",
    limit: int = 20,
    offset: int = 0,
) -> HTMLResponse:
    """Search results partial for htmx swap."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    results = None
    if q.strip():
        results = await _archive_repo.search(
            conn, q, limit=limit, offset=offset
        )
    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {"query": q, "results": results, "limit": limit, "offset": offset},
    )
