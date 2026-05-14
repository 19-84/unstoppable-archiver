# ABOUTME: Unit tests for the custom exception hierarchy
# ABOUTME: Validates error codes, messages, defaults, and inheritance chains
"""Tests for error classes."""

from __future__ import annotations

from archiver.errors import (
    AntiBotDetectedError,
    AppError,
    CaptureError,
    DuplicateCaptureError,
)


class TestAppError:
    def test_message_and_code(self) -> None:
        err = AppError("something broke", "MY_CODE")
        assert err.message == "something broke"
        assert err.code == "MY_CODE"
        assert str(err) == "something broke"

    def test_default_code(self) -> None:
        err = AppError("msg")
        assert err.code == "UNKNOWN"


class TestCaptureError:
    def test_defaults(self) -> None:
        err = CaptureError()
        assert err.code == "CAPTURE_ERROR"

    def test_inherits_app_error(self) -> None:
        assert isinstance(CaptureError(), AppError)


class TestAntiBotDetectedError:
    def test_code_override(self) -> None:
        err = AntiBotDetectedError()
        assert err.code == "ANTI_BOT_DETECTED"

    def test_custom_message(self) -> None:
        err = AntiBotDetectedError("Cloudflare blocked")
        assert err.message == "Cloudflare blocked"

    def test_inherits_capture_error(self) -> None:
        assert isinstance(AntiBotDetectedError(), CaptureError)
        assert isinstance(AntiBotDetectedError(), AppError)


class TestDuplicateCaptureError:
    def test_existing_id(self) -> None:
        err = DuplicateCaptureError(
            "already captured", existing_id="01ABC"
        )
        assert err.existing_id == "01ABC"
        assert err.code == "DUPLICATE_CAPTURE"

    def test_default_existing_id(self) -> None:
        err = DuplicateCaptureError()
        assert err.existing_id == ""

    def test_inherits_app_error(self) -> None:
        assert isinstance(DuplicateCaptureError(), AppError)
