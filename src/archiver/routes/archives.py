# ABOUTME: REST API routes for archive CRUD, search, and artifact serving
# ABOUTME: Handles URL submission, status polling, FTS search, and file downloads
"""Archive API routes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from archiver.blocklist import DomainBlocklist
from archiver.deps import get_blocklist, get_client_ip_hash, get_db, require_api_key
from archiver.enums import CaptureTier
from archiver.errors import DuplicateCaptureError
from archiver.models import (
    ArchiveCreate,
    ArchiveListResponse,
    ArchiveRecord,
    SearchResult,
)
from archiver.rate_limit import enforce_limit
from archiver.repository import ArchiveRepository, JobRepository, PgConnection
from archiver.url import url_hash

log = structlog.get_logger()

router = APIRouter(prefix="/archives", tags=["archives"])

_archive_repo = ArchiveRepository()
_job_repo = JobRepository()


@router.post("", status_code=201, response_model=ArchiveRecord)
async def create_archive(
    body: ArchiveCreate,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    blocklist: Annotated[DomainBlocklist, Depends(get_blocklist)],
) -> ArchiveRecord:
    """Submit a URL for archiving."""
    settings = request.app.state.settings
    enforce_limit(request, settings.rate_limit_submit_per_hour)
    from archiver.url_safety import check_url_safety

    safety_error = check_url_safety(str(body.url), blocklist=blocklist)
    if safety_error:
        raise HTTPException(status_code=400, detail=safety_error)

    uhash = url_hash(str(body.url))

    # Dedup level 2: check recapture interval
    if not body.force:
        recent = await _archive_repo.check_recent_capture(
            conn, uhash, settings.recapture_interval_seconds
        )
        if recent is not None:
            raise DuplicateCaptureError(
                f"URL captured {recent.completed_at}, use force=true to override",
                existing_id=recent.id,
            )

    archive = await _archive_repo.create(
        conn, str(body.url), submitter_ip_hash=get_client_ip_hash(request)
    )
    await _job_repo.enqueue(
        conn, archive.id, CaptureTier.CHROMIUM, priority=body.priority
    )
    return archive


@router.get("", response_model=ArchiveListResponse)
async def list_archives(
    conn: Annotated[PgConnection, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ArchiveListResponse:
    """List archives, most recent first."""
    archives, total = await _archive_repo.list_recent(
        conn, limit=limit, offset=offset
    )
    return ArchiveListResponse(
        archives=archives, total=total, limit=limit, offset=offset,
    )


@router.get("/search", response_model=SearchResult)
async def search_archives(
    q: str,
    conn: Annotated[PgConnection, Depends(get_db)],
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> SearchResult:
    """Full-text search across archived pages."""
    result = await _archive_repo.search(
        conn, q, limit=limit, offset=offset
    )
    # The repo's SearchResult uses defaults for limit/offset; overwrite
    # with the actual values the caller used so the response is self-
    # describing for paginating API consumers.
    return result.model_copy(update={"limit": limit, "offset": offset})


@router.get("/{archive_id}", response_model=ArchiveRecord)
async def get_archive(
    archive_id: str,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> ArchiveRecord:
    """Get a single archive by ID."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    return archive


@router.delete(
    "/{archive_id}",
    status_code=204,
    dependencies=[Depends(require_api_key)],
)
async def delete_archive(
    archive_id: str,
    conn: Annotated[PgConnection, Depends(get_db)],
    request: Request,
) -> None:
    """Delete archive record and its artifacts."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")

    # Delete artifacts from disk
    if archive.artifact_dir:
        base = Path(request.app.state.settings.artifacts_dir).resolve()
        artifact_path = (base / archive.artifact_dir).resolve()
        if artifact_path.is_relative_to(base) and artifact_path.exists():
            import shutil

            shutil.rmtree(artifact_path)

    await _archive_repo.delete(conn, archive_id)


def _get_artifact_path(
    archive: ArchiveRecord,
    filename: str,
    settings_artifacts_dir: Path,
) -> Path:
    """Resolve artifact path, raising 404 if not found."""
    if not archive.artifact_dir:
        raise HTTPException(
            status_code=404,
            detail="Archive has no artifacts",
        )
    base = settings_artifacts_dir.resolve()
    path = (base / archive.artifact_dir / filename).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(
            status_code=400, detail="Invalid artifact path"
        )
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Artifact {filename} not found",
        )
    return path


_SNAPSHOT_HEADERS = {
    "Content-Security-Policy": (
        "sandbox; default-src 'none'; style-src 'unsafe-inline';"
        " img-src data: blob:"
    ),
    "X-Content-Type-Options": "nosniff",
}


@router.get("/{archive_id}/snapshot")
async def get_snapshot(
    archive_id: str,
    conn: Annotated[PgConnection, Depends(get_db)],
    request: Request,
) -> Response:
    """Serve the archived HTML snapshot.

    New archives are written as `snapshot.html.zst` (zstd level 19,
    ~5x smaller on plain HTML). The serve path picks:

    1. If `snapshot.html.zst` exists AND the client advertised zstd in
       `Accept-Encoding`: stream the raw .zst bytes with
       `Content-Encoding: zstd` — modern browsers (Chrome/Edge ≥125,
       Firefox ≥126) decode natively, no server CPU.
    2. If `snapshot.html.zst` exists but the client didn't ask for zstd
       (curl, old Safari, etc.): decompress server-side and serve plain.
    3. Otherwise fall back to a legacy uncompressed `snapshot.html`
       (pre-compression captures).
    """
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    if not archive.artifact_dir:
        raise HTTPException(
            status_code=404, detail="Archive has no artifacts",
        )

    artifacts_dir: Path = request.app.state.settings.artifacts_dir
    base = artifacts_dir.resolve()
    archive_dir = (base / archive.artifact_dir).resolve()
    if not archive_dir.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Invalid artifact path")

    zst_path = archive_dir / "snapshot.html.zst"
    plain_path = archive_dir / "snapshot.html"
    accept_encoding = request.headers.get("accept-encoding", "")
    client_supports_zstd = "zstd" in accept_encoding.lower()

    if zst_path.exists():
        if client_supports_zstd:
            return FileResponse(
                zst_path,
                media_type="text/html",
                headers={
                    **_SNAPSHOT_HEADERS,
                    "Content-Encoding": "zstd",
                },
            )
        # Server-side decompress fallback for old clients
        import zstandard as _zstd
        decompressor = _zstd.ZstdDecompressor()
        plain_bytes = decompressor.decompress(zst_path.read_bytes())
        return Response(
            content=plain_bytes,
            media_type="text/html",
            headers=_SNAPSHOT_HEADERS,
        )

    if plain_path.exists():
        return FileResponse(
            plain_path,
            media_type="text/html",
            headers=_SNAPSHOT_HEADERS,
        )

    raise HTTPException(
        status_code=404, detail="Artifact snapshot.html not found",
    )


@router.get("/{archive_id}/warc")
async def get_warc(
    archive_id: str,
    conn: Annotated[PgConnection, Depends(get_db)],
    request: Request,
) -> FileResponse:
    """Serve the WARC archive file."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    path = _get_artifact_path(
        archive, "archive.warc.gz", request.app.state.settings.artifacts_dir
    )
    return FileResponse(
        path,
        media_type="application/warc",
        filename=f"{archive_id}.warc.gz",
    )


@router.get("/{archive_id}/screenshot")
async def get_screenshot(
    archive_id: str,
    conn: Annotated[PgConnection, Depends(get_db)],
    request: Request,
) -> FileResponse:
    """Serve the screenshot PNG."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    path = _get_artifact_path(
        archive, "screenshot.png", request.app.state.settings.artifacts_dir
    )
    return FileResponse(path, media_type="image/png")


@router.get("/{archive_id}/thumbnail")
async def get_thumbnail(
    archive_id: str,
    conn: Annotated[PgConnection, Depends(get_db)],
    request: Request,
) -> FileResponse:
    """Serve the thumbnail PNG."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")
    path = _get_artifact_path(
        archive, "thumbnail.png", request.app.state.settings.artifacts_dir
    )
    return FileResponse(path, media_type="image/png")
