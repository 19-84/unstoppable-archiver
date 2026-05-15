# ABOUTME: Admin routes — login, dashboard, reports moderation, audit log, archive management
# ABOUTME: Password-based auth via bcrypt + session cookie; all routes gated by require_admin
"""Admin routes for moderation and system management."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from archiver.auth import require_admin_redirect, verify_password
from archiver.blocklist import load_blocklist
from archiver.config import Settings
from archiver.deps import get_client_ip_hash, get_db, get_settings
from archiver.enums import AuditAction, ReportStatus
from archiver.repository import (
    ArchiveRepository,
    AuditRepository,
    PgConnection,
    ReportRepository,
)

log = structlog.get_logger()

router = APIRouter(prefix="/admin", tags=["admin"])

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

_archive_repo = ArchiveRepository()
_audit_repo = AuditRepository()
_report_repo = ReportRepository()


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_form(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    next: str = "/admin/",
) -> HTMLResponse:
    """Render the admin login form."""
    if not settings.admin_enabled:
        raise HTTPException(status_code=404)
    if request.session.get("admin"):
        return RedirectResponse(url=next, status_code=303)  # type: ignore[return-value]
    return templates.TemplateResponse(
        request, "admin/login.html", {"next": next, "error": None}
    )


@router.post("/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/admin/",
) -> HTMLResponse | RedirectResponse:
    """Authenticate admin password and set session."""
    if not settings.admin_enabled:
        raise HTTPException(status_code=404)

    ip = get_client_ip_hash(request)
    if verify_password(
        password, settings.admin_password_hash.get_secret_value()
    ):
        request.session["admin"] = True
        await _audit_repo.log(
            conn, AuditAction.ADMIN_LOGIN, admin_user="admin", ip_address_hash=ip
        )
        # Prevent open-redirect: only allow relative paths
        target = next if next.startswith("/") and not next.startswith("//") else "/admin/"
        return RedirectResponse(url=target, status_code=303)

    await _audit_repo.log(
        conn, AuditAction.ADMIN_LOGIN_FAILED, admin_user="admin", ip_address_hash=ip
    )
    return templates.TemplateResponse(
        request,
        "admin/login.html",
        {"next": next, "error": "Invalid password"},
        status_code=401,
    )


@router.post("/logout")
async def logout(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> RedirectResponse:
    """Clear the admin session."""
    if request.session.get("admin"):
        await _audit_repo.log(
            conn,
            AuditAction.ADMIN_LOGOUT,
            admin_user="admin",
            ip_address_hash=get_client_ip_hash(request),
        )
    request.session.clear()
    return RedirectResponse(url="/admin/login", status_code=303)


@router.get("/", response_class=HTMLResponse, response_model=None)
async def dashboard(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
) -> HTMLResponse | RedirectResponse:
    """Admin dashboard showing pending moderation and recent activity."""
    if isinstance(_admin, RedirectResponse):
        return _admin

    pending_reports = await _report_repo.count_pending(conn)
    recent_archives, total_archives = await _archive_repo.list_recent(
        conn, limit=5, show_removed=True
    )
    blocklist = request.app.state.blocklist

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "pending_reports": pending_reports,
            "recent_archives": recent_archives,
            "total_archives": total_archives,
            "blocklist": blocklist,
        },
    )


@router.get("/reports", response_class=HTMLResponse, response_model=None)
async def reports_list(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
    status: str = "pending",
) -> HTMLResponse | RedirectResponse:
    """List abuse reports filtered by status."""
    if isinstance(_admin, RedirectResponse):
        return _admin

    status_filter: ReportStatus | None
    try:
        status_filter = ReportStatus(status) if status != "all" else None
    except ValueError:
        status_filter = ReportStatus.PENDING

    reports = await _report_repo.list_by_status(conn, status_filter, limit=100)
    return templates.TemplateResponse(
        request,
        "admin/reports.html",
        {"reports": reports, "status_filter": status, "ReportStatus": ReportStatus},
    )


@router.post("/reports/{report_id}/resolve", response_model=None)
async def resolve_report(
    report_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
    action: Annotated[str, Form()] = "resolve",
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Resolve or dismiss a report, optionally soft-deleting the archive."""
    if isinstance(_admin, RedirectResponse):
        return _admin

    report = await _report_repo.get_by_id(conn, report_id)
    if report is None:
        raise HTTPException(status_code=404)

    ip = get_client_ip_hash(request)

    if action == "dismiss":
        await _report_repo.update_status(
            conn, report_id, ReportStatus.DISMISSED,
            resolved_by="admin", notes=notes.strip() or None,
        )
        await _audit_repo.log(
            conn, AuditAction.REPORT_DISMISSED, archive_id=report.archive_id,
            admin_user="admin", ip_address_hash=ip,
            details={"report_id": report_id, "notes": notes},
        )
    else:  # resolve (takedown)
        await _archive_repo.soft_delete(
            conn, report.archive_id,
            reason=f"Report {report_id}: {notes[:200]}",
        )
        await _report_repo.update_status(
            conn, report_id, ReportStatus.RESOLVED,
            resolved_by="admin", notes=notes.strip() or None,
        )
        await _audit_repo.log(
            conn, AuditAction.ARCHIVE_SOFT_DELETE,
            archive_id=report.archive_id,
            admin_user="admin", ip_address_hash=ip,
            details={"report_id": report_id, "notes": notes},
        )
        await _audit_repo.log(
            conn, AuditAction.REPORT_RESOLVED, archive_id=report.archive_id,
            admin_user="admin", ip_address_hash=ip,
            details={"report_id": report_id},
        )

    return RedirectResponse(url="/admin/reports", status_code=303)


@router.get("/archives", response_class=HTMLResponse, response_model=None)
async def archives_list(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
    limit: int = 50,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """List all archives including removed ones."""
    if isinstance(_admin, RedirectResponse):
        return _admin

    archives, total = await _archive_repo.list_recent(
        conn, limit=limit, offset=offset, show_removed=True,
    )
    return templates.TemplateResponse(
        request,
        "admin/archives.html",
        {
            "archives": archives, "total": total,
            "limit": limit, "offset": offset,
        },
    )


@router.post("/archives/{archive_id}/remove", response_model=None)
async def admin_remove_archive(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
    reason: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Soft-delete an archive."""
    if isinstance(_admin, RedirectResponse):
        return _admin

    await _archive_repo.soft_delete(
        conn, archive_id, reason=reason.strip() or "admin removal"
    )
    await _audit_repo.log(
        conn, AuditAction.ARCHIVE_SOFT_DELETE, archive_id=archive_id,
        admin_user="admin", ip_address_hash=get_client_ip_hash(request),
        details={"reason": reason},
    )
    return RedirectResponse(url="/admin/archives", status_code=303)


@router.post("/archives/{archive_id}/restore", response_model=None)
async def admin_restore_archive(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
) -> RedirectResponse:
    """Restore a soft-deleted archive."""
    if isinstance(_admin, RedirectResponse):
        return _admin

    await _archive_repo.restore(conn, archive_id)
    await _audit_repo.log(
        conn, AuditAction.ARCHIVE_RESTORE, archive_id=archive_id,
        admin_user="admin", ip_address_hash=get_client_ip_hash(request),
    )
    return RedirectResponse(url="/admin/archives", status_code=303)


@router.post("/archives/{archive_id}/hard-delete", response_model=None)
async def admin_hard_delete_archive(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
) -> RedirectResponse:
    """Permanently delete an archive and its artifacts."""
    if isinstance(_admin, RedirectResponse):
        return _admin

    # include_removed=True: hard-delete typically targets archives
    # that were already soft-deleted via takedown. Without the flag
    # the public-safe SELECT would return None and we'd silently
    # skip the on-disk cleanup.
    archive = await _archive_repo.get_by_id(
        conn, archive_id, include_removed=True,
    )
    if archive and archive.artifact_dir:
        artifact_path = settings.artifacts_dir / archive.artifact_dir
        base = settings.artifacts_dir.resolve()
        if artifact_path.resolve().is_relative_to(base) and artifact_path.exists():
            shutil.rmtree(artifact_path, ignore_errors=True)

    await _archive_repo.delete(conn, archive_id)
    await _audit_repo.log(
        conn, AuditAction.ARCHIVE_HARD_DELETE, archive_id=archive_id,
        admin_user="admin", ip_address_hash=get_client_ip_hash(request),
    )
    return RedirectResponse(url="/admin/archives", status_code=303)


@router.post("/blocklist/reload", response_model=None)
async def reload_blocklist(
    request: Request,
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
) -> RedirectResponse:
    """Reload the domain blocklist + allowlist from configured sources."""
    if isinstance(_admin, RedirectResponse):
        return _admin
    settings = get_settings(request)
    request.app.state.blocklist = await load_blocklist(settings)
    return RedirectResponse(url="/admin/", status_code=303)


@router.get("/audit", response_class=HTMLResponse, response_model=None)
async def audit_log_view(
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    _admin: Annotated[str | RedirectResponse, Depends(require_admin_redirect)],
    limit: int = 100,
    offset: int = 0,
) -> HTMLResponse | RedirectResponse:
    """View the audit log."""
    if isinstance(_admin, RedirectResponse):
        return _admin

    entries = await _audit_repo.list_recent(conn, limit=limit, offset=offset)
    return templates.TemplateResponse(
        request,
        "admin/audit.html",
        {"entries": entries, "limit": limit, "offset": offset},
    )
