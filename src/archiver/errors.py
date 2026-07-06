# ABOUTME: Custom exception hierarchy rooted at AppError
# ABOUTME: Used by capture pipeline, worker, and API for structured error handling
"""Custom exception hierarchy for unstoppable-archive."""

from __future__ import annotations


class AppError(Exception):
    """Base exception for all application errors."""

    def __init__(self, message: str, code: str = "UNKNOWN") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class CaptureError(AppError):
    """Error during page capture."""

    def __init__(self, message: str = "Capture failed") -> None:
        super().__init__(message, code="CAPTURE_ERROR")


class AntiBotDetectedError(CaptureError):
    """Anti-bot protection detected on target page."""

    def __init__(self, message: str = "Anti-bot protection detected") -> None:
        super().__init__(message)
        self.code = "ANTI_BOT_DETECTED"


class DuplicateCaptureError(AppError):
    """URL was recently captured and re-capture interval has not elapsed."""

    def __init__(self, message: str = "URL recently captured", existing_id: str = "") -> None:
        super().__init__(message, code="DUPLICATE_CAPTURE")
        self.existing_id = existing_id


class FetchError(AppError):
    """Error during an outbound HTTP fetch (see archiver.http_client)."""

    def __init__(self, message: str, code: str = "FETCH_ERROR") -> None:
        super().__init__(message, code=code)


class UpstreamError(FetchError):
    """Network-level failure that persisted through all retry attempts."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UPSTREAM_ERROR")


class BodyTooLargeError(FetchError):
    """Response body exceeded the caller's byte budget."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="BODY_TOO_LARGE")


class UnsafeURLError(FetchError):
    """Fetch target (or a redirect hop) failed the SSRF safety check."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="UNSAFE_URL")


