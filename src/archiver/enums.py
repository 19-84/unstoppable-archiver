# ABOUTME: Status enumerations for archive lifecycle, job queue, and capture tiers
# ABOUTME: Defines the state machines and escalation order used by worker and repository
"""Status enumerations for archive and job state machines."""

from __future__ import annotations

from enum import StrEnum


class ArchiveStatus(StrEnum):
    """Archive lifecycle states."""

    PENDING = "pending"
    CAPTURING = "capturing"
    COMPLETE = "complete"
    FAILED = "failed"


class JobStatus(StrEnum):
    """Job queue states."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    RETRY = "retry"


class CaptureTier(StrEnum):
    """Capture escalation tiers, ordered from fastest to most resilient."""

    CHROMIUM = "chromium"
    CAMOUFOX = "camoufox"
    CAMOUFOX_PROXY = "camoufox_proxy"
    WAYBACK = "wayback"
    ARCHIVE_TODAY = "archive_today"
    COMMONCRAWL = "commoncrawl"


class CaptureSource(StrEnum):
    """Where the archived content was retrieved from."""

    DIRECT = "direct"
    WAYBACK = "wayback"
    ARCHIVE_TODAY = "archive_today"
    COMMONCRAWL = "commoncrawl"


class NetworkType(StrEnum):
    """Network classification for URL routing."""

    CLEARNET = "clearnet"
    TOR = "tor"
    I2P = "i2p"


class AuditAction(StrEnum):
    """Admin and system actions tracked in the audit log."""

    ARCHIVE_SOFT_DELETE = "archive_soft_delete"
    ARCHIVE_HARD_DELETE = "archive_hard_delete"
    ARCHIVE_RESTORE = "archive_restore"
    REPORT_RESOLVED = "report_resolved"
    REPORT_DISMISSED = "report_dismissed"
    ADMIN_LOGIN = "admin_login"
    ADMIN_LOGIN_FAILED = "admin_login_failed"
    ADMIN_LOGOUT = "admin_logout"


class ReportReason(StrEnum):
    """Reasons for reporting an archived page."""

    COPYRIGHT = "copyright"
    PERSONAL_INFO = "personal_info"
    MALICIOUS = "malicious"
    OTHER = "other"


class ReportStatus(StrEnum):
    """Abuse report lifecycle states."""

    PENDING = "pending"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


# Tier escalation order for clearnet URLs. Common Crawl is last —
# staleness (1-3 months) means it's strictly worse than Wayback when
# both have coverage; it earns its place by catching long-tail URLs
# (indie blogs, abandoned sites) that Wayback often missed.
CLEARNET_TIER_ORDER: list[CaptureTier] = [
    CaptureTier.CHROMIUM,
    CaptureTier.CAMOUFOX,
    CaptureTier.CAMOUFOX_PROXY,
    CaptureTier.WAYBACK,
    CaptureTier.ARCHIVE_TODAY,
    CaptureTier.COMMONCRAWL,
]

# Tier escalation order for darknet URLs (no indirect tiers)
DARKNET_TIER_ORDER: list[CaptureTier] = [
    CaptureTier.CAMOUFOX,
]
