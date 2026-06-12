# ABOUTME: Admin authentication via bcrypt-hashed password + session cookie
# ABOUTME: Single-admin model; password hash in env var, session signed by SessionMiddleware
"""Admin authentication helpers."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

import bcrypt
import structlog
from beartype import beartype
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from archiver.config import Settings
from archiver.deps import get_settings

log = structlog.get_logger()


@beartype
def verify_password(plain: str, hashed: str) -> bool:
    """Check a plaintext password against a bcrypt hash."""
    if not hashed or not plain:
        return False
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8"), hashed.encode("utf-8")
        )
    except ValueError:
        return False


@beartype
def hash_password(plain: str) -> str:
    """Hash a password with bcrypt (for use in setup CLI)."""
    return bcrypt.hashpw(
        plain.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")


@beartype
def safe_next_path(next_path: str) -> str:
    """Clamp a post-login redirect target to a local path.

    Rejects anything that isn't a plain same-origin path: absolute
    URLs, scheme-relative //host, and backslash variants (browsers
    normalize /\\host to //host, so a bare startswith("//") check is
    bypassable). Falls back to the dashboard.
    """
    if (
        next_path.startswith("/")
        and not next_path.startswith("//")
        and "\\" not in next_path
    ):
        return next_path
    return "/admin/"


async def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """FastAPI dependency: raise 401 redirect if not an admin session."""
    if not settings.admin_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Admin interface not configured",
        )
    if not request.session.get("admin"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login required",
            headers={
                "Location": f"/admin/login?next={quote(request.url.path, safe='/')}"
            },
        )
    return "admin"


async def require_admin_redirect(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> str | RedirectResponse:
    """For HTML routes: redirect to login instead of raising 401."""
    if not settings.admin_enabled:
        raise HTTPException(status_code=404)
    if not request.session.get("admin"):
        return RedirectResponse(
            url=f"/admin/login?next={quote(request.url.path, safe='/')}",
            status_code=303,
        )
    return "admin"
