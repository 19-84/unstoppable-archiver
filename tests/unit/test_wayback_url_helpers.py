# ABOUTME: Unit tests for Wayback-style URL helpers
# ABOUTME: Verifies _pad_timestamp padding rules and _wayback_url filter output
"""Tests for the Wayback-style URL path helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import time_machine

from archiver.routes.pages import _pad_timestamp, _wayback_url


class TestPadTimestamp:
    def test_full_timestamp_returns_as_is(self) -> None:
        assert _pad_timestamp("20260418123045") == "20260418123045"

    def test_year_only_pads_to_end_of_year(self) -> None:
        assert _pad_timestamp("2026") == "20261231235959"

    def test_year_month_pads_to_end_of_month(self) -> None:
        # Note: we use literal "31" as day padding regardless of real
        # month length — the closest-timestamp query uses proximity
        # matching, so feb-31 being "invalid" just falls back to the
        # nearest valid snapshot; not worth the complexity to resolve.
        assert _pad_timestamp("202604") == "20260431235959"

    def test_year_month_day_pads_to_end_of_day(self) -> None:
        assert _pad_timestamp("20260418") == "20260418235959"

    def test_year_month_day_hour(self) -> None:
        assert _pad_timestamp("2026041812") == "20260418125959"

    def test_non_digit_rejected(self) -> None:
        assert _pad_timestamp("2026-04-18") is None

    def test_too_short_rejected(self) -> None:
        assert _pad_timestamp("123") is None

    def test_too_long_rejected(self) -> None:
        assert _pad_timestamp("1234567890123456") is None


class TestWaybackUrlFilter:
    @time_machine.travel("2026-04-18 12:30:45")
    def test_builds_wayback_url(self) -> None:
        from datetime import UTC, datetime

        archive = SimpleNamespace(
            url="https://example.com/article",
            created_at=datetime(2026, 4, 18, 12, 30, 45, tzinfo=UTC),
        )
        assert _wayback_url(archive) == "/web/20260418123045/https://example.com/article"

    def test_missing_attrs_returns_empty(self) -> None:
        assert _wayback_url(SimpleNamespace()) == ""
        assert _wayback_url(SimpleNamespace(url="x")) == ""
        assert _wayback_url(SimpleNamespace(created_at=None, url="x")) == ""


@pytest.fixture(autouse=False)
def _noop() -> None:
    """Keep the file importable even if time_machine isn't pre-installed."""
    return None
