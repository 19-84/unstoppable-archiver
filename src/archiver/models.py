# ABOUTME: Pydantic v2 domain models for archives, jobs, and capture results
# ABOUTME: Shared by repository layer, API routes, and worker for type-safe data exchange
"""Pydantic v2 domain models for archives and jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, HttpUrl

from archiver.enums import (
    ArchiveStatus,
    AuditAction,
    CaptureSource,
    CaptureTier,
    JobStatus,
    ReportReason,
    ReportStatus,
)


class ArchiveCreate(BaseModel):
    """Request model for creating a new archive."""

    model_config = ConfigDict(strict=True, extra="forbid")

    url: HttpUrl
    priority: int = 0
    force: bool = False


class ArchiveRecord(BaseModel):
    """Database record for an archived page."""

    model_config = ConfigDict(strict=True, from_attributes=True, extra="forbid")

    id: str
    url: str
    url_hash: str
    title: str | None = None
    text_content: str | None = None
    status: ArchiveStatus
    tier: CaptureTier
    source: CaptureSource = CaptureSource.DIRECT
    error_message: str | None = None
    artifact_dir: str | None = None
    content_hash: str | None = None
    screenshot_hash: str | None = None
    revisit_of: str | None = None
    snapshot_size: int | None = None
    warc_size: int | None = None
    created_at: datetime
    completed_at: datetime | None = None
    removed_at: datetime | None = None
    removed_reason: str | None = None


class JobRecord(BaseModel):
    """Database record for a capture job."""

    model_config = ConfigDict(strict=True, from_attributes=True, extra="forbid")

    id: str
    archive_id: str
    status: JobStatus
    tier: CaptureTier
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 3
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    locked_by: str | None = None
    locked_at: datetime | None = None


class SearchResult(BaseModel):
    """Paginated search results."""

    model_config = ConfigDict(strict=True, extra="forbid")

    archives: list[ArchiveRecord]
    total: int
    query: str


class ArchiveListResponse(BaseModel):
    """Paginated archive list."""

    model_config = ConfigDict(strict=True, extra="forbid")

    archives: list[ArchiveRecord]
    total: int


class AuditLogEntry(BaseModel):
    """Audit log entry for admin actions and system events."""

    model_config = ConfigDict(strict=True, from_attributes=True, extra="forbid")

    id: str
    created_at: datetime
    action: AuditAction
    archive_id: str | None = None
    admin_user: str | None = None
    ip_address: str | None = None
    details: dict | None = None  # type: ignore[type-arg]


class ReportCreate(BaseModel):
    """Request model for submitting an abuse report."""

    model_config = ConfigDict(strict=True, extra="forbid")

    reason: ReportReason
    details: str | None = None
    reporter_email: str | None = None


class ReportRecord(BaseModel):
    """Database record for an abuse report."""

    model_config = ConfigDict(strict=True, from_attributes=True, extra="forbid")

    id: str
    archive_id: str
    reason: ReportReason
    details: str | None = None
    reporter_email: str | None = None
    reporter_ip: str | None = None
    created_at: datetime
    status: ReportStatus = ReportStatus.PENDING
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_notes: str | None = None


@dataclass(frozen=True)
class CaptureResult:
    """Output from a single capture pipeline run."""

    snapshot_html: bytes
    screenshot_png: bytes
    thumbnail_png: bytes
    text_content: str
    title: str
    warc_path: Path | None
    warc_size: int
    content_hash: str
    screenshot_hash: str
