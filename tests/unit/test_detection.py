# ABOUTME: Unit tests for anti-bot detection heuristics
# ABOUTME: Verifies Cloudflare, CAPTCHA, and status code detection without real browsers
"""Tests for anti-bot detection."""

from __future__ import annotations

from archiver.detection import check_anti_bot


class TestCheckAntiBot:
    def test_normal_page_not_blocked(self) -> None:
        signal = check_anti_bot(
            200, "Example Page", "Hello world " * 100
        )
        assert signal.is_blocked is False

    def test_403_blocked(self) -> None:
        signal = check_anti_bot(403, "Forbidden", "Access denied")
        assert signal.is_blocked is True
        assert "403" in (signal.reason or "")

    def test_429_blocked(self) -> None:
        signal = check_anti_bot(429, "Rate Limited", "Too many")
        assert signal.is_blocked is True

    def test_503_blocked(self) -> None:
        signal = check_anti_bot(503, "Unavailable", "Service down")
        assert signal.is_blocked is True

    def test_200_normal_not_blocked(self) -> None:
        signal = check_anti_bot(200, "My Blog", "x" * 1000)
        assert signal.is_blocked is False

    def test_cloudflare_title_detected(self) -> None:
        signal = check_anti_bot(
            200, "Just a moment...", "Please wait"
        )
        assert signal.is_blocked is True
        assert "just a moment" in (signal.reason or "")

    def test_attention_required_title(self) -> None:
        signal = check_anti_bot(
            200, "Attention Required", "Cloudflare"
        )
        assert signal.is_blocked is True

    def test_cloudflare_body_marker(self) -> None:
        signal = check_anti_bot(
            200,
            "Some Page",
            "cf-browser-verification challenge " * 5,
        )
        assert signal.is_blocked is True
        assert "Cloudflare" in (signal.reason or "")

    def test_turnstile_marker(self) -> None:
        signal = check_anti_bot(
            200,
            "Login",
            "Please complete the cf-turnstile challenge",
        )
        assert signal.is_blocked is True

    def test_captcha_in_short_body(self) -> None:
        signal = check_anti_bot(
            200,
            "Verify",
            "Please solve the captcha below",
        )
        assert signal.is_blocked is True

    def test_captcha_in_long_body_not_blocked(self) -> None:
        # A real page mentioning "captcha" in passing shouldn't trigger
        long_body = "This article discusses captcha " + "x" * 1000
        signal = check_anti_bot(200, "Article", long_body)
        assert signal.is_blocked is False

    def test_empty_body_with_ok_title(self) -> None:
        signal = check_anti_bot(200, "Page", "")
        assert signal.is_blocked is False

    def test_access_denied_title(self) -> None:
        signal = check_anti_bot(200, "Access Denied", "No access")
        assert signal.is_blocked is True

    def test_case_insensitive_title(self) -> None:
        signal = check_anti_bot(
            200, "CHECKING YOUR BROWSER", "Wait..."
        )
        assert signal.is_blocked is True
