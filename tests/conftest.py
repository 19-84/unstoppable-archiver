# ABOUTME: Shared pytest fixtures for all test suites
# ABOUTME: Provides database connections, test servers, and common helpers
"""Shared test fixtures for unstoppable-archive."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _disable_singlefile_cli() -> object:  # type: ignore[misc]
    """Force JS-based SingleFile in all tests (CLI subprocess not mockable)."""
    with patch("archiver.capture.cli_available", return_value=False):
        yield
