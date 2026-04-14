# ABOUTME: Unit tests for worker tier escalation and next_tier logic
# ABOUTME: Tests the pure escalation function without requiring databases or browsers
"""Tests for worker tier escalation logic."""

from __future__ import annotations

from archiver.enums import CaptureTier
from archiver.worker import next_tier


class TestNextTier:
    def test_chromium_escalates_to_camoufox(self) -> None:
        assert next_tier(CaptureTier.CHROMIUM) == CaptureTier.CAMOUFOX

    def test_camoufox_escalates_to_proxy(self) -> None:
        assert (
            next_tier(CaptureTier.CAMOUFOX)
            == CaptureTier.CAMOUFOX_PROXY
        )

    def test_proxy_escalates_to_wayback(self) -> None:
        assert (
            next_tier(CaptureTier.CAMOUFOX_PROXY)
            == CaptureTier.WAYBACK
        )

    def test_wayback_escalates_to_archive_today(self) -> None:
        assert (
            next_tier(CaptureTier.WAYBACK)
            == CaptureTier.ARCHIVE_TODAY
        )

    def test_archive_today_returns_none(self) -> None:
        assert next_tier(CaptureTier.ARCHIVE_TODAY) is None

    def test_full_chain_has_five_tiers(self) -> None:
        """The escalation chain has 5 tiers total (4 escalation steps)."""
        tiers: list[CaptureTier] = [CaptureTier.CHROMIUM]
        tier: CaptureTier | None = CaptureTier.CHROMIUM
        while (tier := next_tier(tier)) is not None:
            tiers.append(tier)
        total_tiers = 5
        assert len(tiers) == total_tiers
