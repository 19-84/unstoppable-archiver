# ABOUTME: HTML page routes for the Glass Noir frontend
# ABOUTME: Serves Jinja2 templates with htmx progressive enhancement
"""HTML page routes — server-rendered with htmx enhancement."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from archiver.blocklist import DomainBlocklist
from archiver.deps import (
    get_blocklist,
    get_client_ip,
    get_db,
    get_settings,
)
from archiver.enums import ArchiveStatus
from archiver.rate_limit import enforce_limit
from archiver.repository import ArchiveRepository, PgConnection

router = APIRouter(tags=["pages"])

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

_archive_repo = ArchiveRepository()


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
        " count(*) FILTER (WHERE status = 'complete') as complete_count"
        " FROM archives WHERE removed_at IS NULL"
    )
    total = stats_row["total_pages"] if stats_row else 0
    complete = stats_row["complete_count"] if stats_row else 0
    stats = {
        "total_pages": total,
        "total_domains": stats_row["total_domains"] if stats_row else 0,
        "storage_mb": round((stats_row["total_bytes"] if stats_row else 0) / 1048576, 1),
        "success_rate": round(complete / total * 100, 1) if total > 0 else 0,
    }
    return templates.TemplateResponse(
        request, "index.html", {"stats": stats}
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

    if not url:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"stats": {}, "error": "Please enter a URL"},
            status_code=400,
        )

    # Detect if input is a search query (no http/https scheme = search)
    if not url.startswith(("http://", "https://")):
        from urllib.parse import quote

        return RedirectResponse(
            url=f"/search?q={quote(url, safe='')}", status_code=303
        )

    from archiver.url_safety import check_url_safety

    safety_error = check_url_safety(url, blocklist=blocklist)
    if safety_error:
        return templates.TemplateResponse(
            request,
            "index.html",
            {"stats": {}, "error": safety_error},
            status_code=400,
        )

    archive = await _archive_repo.create(
        conn, url, submitter_ip=get_client_ip(request)
    )
    job_repo = JobRepository()
    await job_repo.enqueue(conn, archive.id, CaptureTier.CHROMIUM)

    return RedirectResponse(
        url=f"/archive/{archive.id}", status_code=303
    )


@router.get("/archive/{archive_id}", response_class=HTMLResponse)
async def archive_detail(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse:
    """Archive detail page."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")

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


@router.get("/archive/{archive_id}/view", response_class=HTMLResponse)
async def archive_view(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse:
    """Snapshot viewer — sandboxed iframe loads content from /api/archives/{id}/snapshot."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    if archive.status != ArchiveStatus.COMPLETE:
        raise HTTPException(status_code=404, detail="Archive not complete")
    if not archive.artifact_dir:
        raise HTTPException(status_code=404, detail="No artifacts")

    return templates.TemplateResponse(
        request,
        "archive_view.html",
        {"archive": archive},
    )


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
    """Status badge partial for htmx polling."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        return HTMLResponse('<span class="text-xs text-neutral-400">Not found</span>')
    return templates.TemplateResponse(
        request,
        "partials/archive_status.html",
        {"archive": archive},
    )


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
