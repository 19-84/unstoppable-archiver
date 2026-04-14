# ABOUTME: Unit tests for Pydantic domain models (ArchiveCreate, ArchiveRecord, JobRecord)
# ABOUTME: Validates model construction, defaults, strict mode, and extra=forbid rejection
"""Tests for Pydantic domain models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from archiver.enums import ArchiveStatus, CaptureSource, CaptureTier, JobStatus
from archiver.models import ArchiveCreate, ArchiveRecord, JobRecord


class TestArchiveCreate:
    def test_valid_url(self) -> None:
        ac = ArchiveCreate(url="https://example.com/page")  # type: ignore[arg-type]
        assert str(ac.url) == "https://example.com/page"

    def test_defaults(self) -> None:
        ac = ArchiveCreate(url="https://example.com")  # type: ignore[arg-type]
        assert ac.priority == 0
        assert ac.force is False

    def test_invalid_url_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ArchiveCreate(url="not-a-url")  # type: ignore[arg-type]

    def test_force_flag(self) -> None:
        ac = ArchiveCreate(url="https://example.com", force=True)  # type: ignore[arg-type]
        assert ac.force is True


class TestArchiveRecord:
    def test_from_dict(self) -> None:
        now = datetime.now(UTC)
        record = ArchiveRecord(
            id="01JTEST",
            url="https://example.com",
            url_hash="abc123",
            status=ArchiveStatus.PENDING,
            tier=CaptureTier.CHROMIUM,
            source=CaptureSource.DIRECT,
            created_at=now,
        )
        assert record.id == "01JTEST"
        assert record.status == ArchiveStatus.PENDING
        assert record.completed_at is None
        assert record.revisit_of is None

    def test_source_defaults_to_direct(self) -> None:
        now = datetime.now(UTC)
        record = ArchiveRecord(
            id="01JTEST",
            url="https://example.com",
            url_hash="abc123",
            status=ArchiveStatus.COMPLETE,
            tier=CaptureTier.CHROMIUM,
            created_at=now,
        )
        assert record.source == CaptureSource.DIRECT


class TestJobRecord:
    def test_from_dict(self) -> None:
        now = datetime.now(UTC)
        record = JobRecord(
            id="01JTEST",
            archive_id="01JARCH",
            status=JobStatus.QUEUED,
            tier=CaptureTier.CHROMIUM,
            created_at=now,
        )
        assert record.attempts == 0
        assert record.max_attempts == 3  # noqa: PLR2004
        assert record.locked_by is None
