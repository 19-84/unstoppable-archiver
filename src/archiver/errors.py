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


