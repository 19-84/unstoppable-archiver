# ABOUTME: Public abuse report form — no auth, rate-limited
# ABOUTME: Captures copyright/PII/malicious/other reports for admin moderation
"""Public abuse report form routes."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from archiver.captcha import generate_altcha_challenge
from archiver.captcha import verify as verify_captcha
from archiver.deps import get_client_ip, get_db, get_settings
from archiver.enums import ReportReason
from archiver.models import ReportCreate
from archiver.rate_limit import enforce_limit
from archiver.repository import (
    ArchiveRepository,
    PgConnection,
    ReportRepository,
)

router = APIRouter(tags=["reports"])

_templates_dir = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

_archive_repo = ArchiveRepository()
_report_repo = ReportRepository()


@router.get("/report/{archive_id}", response_class=HTMLResponse)
async def report_form(
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
) -> HTMLResponse:
    """Render the abuse report form."""
    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")

    settings = get_settings(request)
    return templates.TemplateResponse(
        request,
        "report_form.html",
        {
            "archive": archive,
            "reasons": list(ReportReason),
            "captcha_provider": settings.captcha_provider,
            "hcaptcha_sitekey": settings.hcaptcha_sitekey,
        },
    )


@router.post("/report/{archive_id}", response_class=HTMLResponse)
async def submit_report(  # noqa: PLR0913
    archive_id: str,
    request: Request,
    conn: Annotated[PgConnection, Depends(get_db)],
    reason: Annotated[str, Form()],
    details: Annotated[str, Form()] = "",
    reporter_email: Annotated[str, Form()] = "",
    captcha_token: Annotated[str, Form(alias="h-captcha-response")] = "",
    altcha_token: Annotated[str, Form(alias="altcha")] = "",
) -> HTMLResponse:
    """Accept an abuse report (no auth)."""
    settings = get_settings(request)
    enforce_limit(request, settings.rate_limit_report_per_hour)

    # Captcha verification (no-op when provider=none)
    token = altcha_token if settings.captcha_provider == "altcha" else captcha_token
    if not await verify_captcha(settings, token):
        raise HTTPException(
            status_code=400, detail="Captcha verification failed"
        )

    archive = await _archive_repo.get_by_id(conn, archive_id)
    if archive is None:
        raise HTTPException(status_code=404, detail="Archive not found")

    try:
        parsed_reason = ReportReason(reason)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=f"Invalid reason: {reason}"
        ) from exc

    report_data = ReportCreate(
        reason=parsed_reason,
        details=details.strip() or None,
        reporter_email=reporter_email.strip() or None,
    )
    await _report_repo.create(
        conn, archive_id, report_data,
        reporter_ip=get_client_ip(request),
    )

    return templates.TemplateResponse(
        request,
        "report_thanks.html",
        {"archive": archive},
    )


@router.get("/captcha/altcha/challenge")
async def altcha_challenge(request: Request) -> JSONResponse:
    """Generate an Altcha proof-of-work challenge."""
    settings = get_settings(request)
    if settings.captcha_provider != "altcha":
        raise HTTPException(status_code=404)
    hmac_key = settings.altcha_hmac_key.get_secret_value()
    if not hmac_key:
        raise HTTPException(
            status_code=500, detail="altcha_hmac_key not configured"
        )
    challenge = generate_altcha_challenge(hmac_key, settings.altcha_max_number)
    return JSONResponse(challenge)
