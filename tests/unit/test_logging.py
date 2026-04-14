# ABOUTME: Unit tests for structured logging setup
# ABOUTME: Verifies JSON and console format modes, invalid level rejection
"""Tests for logging configuration."""

from __future__ import annotations

import pytest
from icontract import ViolationError

from archiver.logging import setup_logging


class TestSetupLogging:
    def test_json_format(self) -> None:
        setup_logging("INFO", "json")

    def test_console_format(self) -> None:
        setup_logging("INFO", "console")

    def test_debug_level(self) -> None:
        setup_logging("DEBUG", "json")

    def test_invalid_level_raises(self) -> None:
        with pytest.raises(ViolationError):
            setup_logging("BOGUS", "json")
