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
    # Site-eligible fallback: rewrite URL to a privacy-frontend
    # instance (Scribe for Medium, xcancel for Twitter, Redlib for
    # Reddit, etc.) and capture through Camoufox + gate-passing
    # SOCKS5. Freshness > completeness — a live frontend render beats
    # a year-old Wayback snapshot for the sites we register. No-ops
    # (escalates immediately) when the URL's apex has no registered
    # frontend.
    PRIVACY_FRONTEND = "privacy_frontend"
    WAYBACK = "wayback"
    ARCHIVE_TODAY = "archive_today"
    COMMONCRAWL = "commoncrawl"
    # Federated Memento (RFC 7089) lookup across national/institutional
    # web archives (arquivo.pt, Archive-It, Australian Web Archive,
    # ...). One timemap query per archive per job; read-only.
    MEMENTO = "memento"
    # Last-resort write path: submit to archive.today on behalf of the
    # user. Slow (30-120 s) and imposes real load on their free service,
    # so we only do it when every read tier has failed to find an
    # existing snapshot.
    ARCHIVE_TODAY_SUBMIT = "archive_today_submit"


class CaptureSource(StrEnum):
    """Where the archived content was retrieved from."""

    DIRECT = "direct"
    WAYBACK = "wayback"
    ARCHIVE_TODAY = "archive_today"
    COMMONCRAWL = "commoncrawl"
    MEMENTO = "memento"
    PRIVACY_FRONTEND = "privacy_frontend"


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


# Tier escalation order for clearnet URLs. Common Crawl and the
# Memento federation come after Wayback/archive.today — staleness
# (1-3 months for CC, years for national-archive collections) means
# they're strictly worse when the big archives have coverage; they earn
# their place by catching long-tail URLs (indie blogs, abandoned sites,
# nationally-scoped content) the big archives missed. The
# archive.today write path stays last: it's the only tier that imposes
# load on a volunteer service, so every read source gets a chance first.
CLEARNET_TIER_ORDER: list[CaptureTier] = [
    CaptureTier.CHROMIUM,
    CaptureTier.CAMOUFOX,
    CaptureTier.CAMOUFOX_PROXY,
    CaptureTier.PRIVACY_FRONTEND,
    CaptureTier.WAYBACK,
    CaptureTier.ARCHIVE_TODAY,
    CaptureTier.COMMONCRAWL,
    CaptureTier.MEMENTO,
    CaptureTier.ARCHIVE_TODAY_SUBMIT,
]

# Tier escalation order for darknet URLs (no indirect tiers)
DARKNET_TIER_ORDER: list[CaptureTier] = [
    CaptureTier.CAMOUFOX,
]
