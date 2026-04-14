# ABOUTME: Pydantic v2 domain models for archives, jobs, and capture results
# ABOUTME: Shared by repository layer, API routes, and worker for type-safe data exchange
"""Pydantic v2 domain models for archives and jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, HttpUrl

from archiver.enums import ArchiveStatus, CaptureSource, CaptureTier, JobStatus


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
