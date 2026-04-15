# ABOUTME: HTML page routes for the Glass Noir frontend
# ABOUTME: Serves Jinja2 templates with htmx progressive enhancement
"""HTML page routes — server-rendered with htmx enhancement."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from archiver.deps import get_db
from archiver.enums import ArchiveStatus
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
    # Gather anonymous stats
    stats_row = await conn.fetchrow(
        "SELECT"
        " count(*) as total_pages,"
        " count(DISTINCT split_part(url, '/', 3)) as total_domains,"
        " coalesce(sum(coalesce(snapshot_size, 0) + coalesce(warc_size, 0)), 0) as total_bytes,"
        " count(*) FILTER (WHERE status = 'complete') as complete_count"
        " FROM archives"
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

    # Get snapshot history for this URL
    history = await _archive_repo.get_by_url_hash(conn, archive.url_hash)

    return templates.TemplateResponse(
        request,
        "archive_detail.html",
        {"archive": archive, "history": history},
    )


@router.get("/archive/{archive_id}/view", response_class=HTMLResponse)
async def archive_view(  # pragma: no cover
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse:
    """Inline snapshot viewer — renders archived HTML with toolbar."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    if archive.status != ArchiveStatus.COMPLETE:
        raise HTTPException(status_code=404, detail="Archive not complete")
    if not archive.artifact_dir:
        raise HTTPException(status_code=404, detail="No artifacts")

    settings = request.app.state.settings
    snapshot_path = (
        Path(settings.artifacts_dir)
        / archive.artifact_dir
        / "snapshot.html"
    )
    if not snapshot_path.exists():
        raise HTTPException(status_code=404, detail="Snapshot file not found")

    snapshot_html = snapshot_path.read_text(encoding="utf-8", errors="replace")

    return templates.TemplateResponse(
        request,
        "archive_view.html",
        {"archive": archive, "snapshot_html": snapshot_html},
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    q: str = "",
) -> HTMLResponse:
    """Search results page."""
    results = None
    if q.strip():
        results = await _archive_repo.search(conn, q)
    return templates.TemplateResponse(
        request,
        "search.html",
        {"query": q, "results": results},
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
) -> HTMLResponse:
    """Search results partial for htmx swap."""
    results = None
    if q.strip():
        results = await _archive_repo.search(conn, q)
    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {"query": q, "results": results},
    )
