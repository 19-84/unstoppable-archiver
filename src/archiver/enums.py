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


class CaptureSource(StrEnum):
    """Where the archived content was retrieved from."""

    DIRECT = "direct"
    WAYBACK = "wayback"
    ARCHIVE_TODAY = "archive_today"


class NetworkType(StrEnum):
    """Network classification for URL routing."""

    CLEARNET = "clearnet"
    TOR = "tor"
    I2P = "i2p"


# Tier escalation order for clearnet URLs
CLEARNET_TIER_ORDER: list[CaptureTier] = [
    CaptureTier.CHROMIUM,
    CaptureTier.CAMOUFOX,
    CaptureTier.CAMOUFOX_PROXY,
    CaptureTier.WAYBACK,
    CaptureTier.ARCHIVE_TODAY,
]

# Tier escalation order for darknet URLs (no indirect tiers)
DARKNET_TIER_ORDER: list[CaptureTier] = [
    CaptureTier.CAMOUFOX,
]
