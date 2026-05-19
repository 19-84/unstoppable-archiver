# ABOUTME: Unit tests for anti-bot detection heuristics
# ABOUTME: Verifies Cloudflare, CAPTCHA, and status code detection without real browsers
"""Tests for anti-bot detection."""

from __future__ import annotations

from archiver.detection import check_anti_bot, detect_js_challenge


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

    def test_twitter_soft_login_wall_detected(self) -> None:
        """X/Twitter serves its logged-out 'log in to continue' wall
        as HTTP 200 with no CAPTCHA — a real captured wall's body.
        Without this the worker stores the 15 MB login-wall page as a
        'successful' direct capture and never escalates to the
        privacy_frontend tier (nitter) that fetches the real tweet.

        Body text below is the actual innerText from a live
        twitter.com/jack/status/20 capture."""
        wall_body = (
            "Don't miss what's happening\n"
            "People on X are the first to know.\n"
            "Log in\nSign up\n"
            "Did someone say … cookies?\n"
            "X and its partners use cookies to provide you with a "
            "better, safer and faster service."
        )
        signal = check_anti_bot(
            200,
            'jack on X: "just setting up my twttr"',
            wall_body,
            has_privacy_frontend=True,
        )
        assert signal.is_blocked is True
        assert "login wall" in (signal.reason or "")

    def test_twitter_login_wall_localized_swedish_detected(self) -> None:
        """X/Twitter localizes the logged-out wall by Accept-Language.
        Observed live: a stealth-browser tier captured the wall in
        Swedish while Chromium got English — the English-only marker
        missed it and the capture wrongly 'succeeded'. The Swedish
        variant body below is the actual innerText from that capture."""
        wall_sv = (
            "Missa inte vad som händer\n"
            "Folk på X får reda på allt först.\n"
            "Logga in\nRegistrera dig\n"
            "Did someone say … cookies?"
        )
        signal = check_anti_bot(
            200, "jack on X", wall_sv, has_privacy_frontend=True
        )
        assert signal.is_blocked is True
        assert "login wall" in (signal.reason or "")

    def test_login_wall_marker_is_specific_no_false_positive(self) -> None:
        """The wall marker must be distinctive enough that an ordinary
        page with a 'Log in' link or generic sign-up copy is NOT
        flagged — escalating a good capture is wasteful. Only the exact
        platform wall phrase trips it."""
        ordinary = (
            "Welcome to our blog. Log in or sign up to comment. "
            "Don't miss our latest posts about gardening." + "x" * 500
        )
        signal = check_anti_bot(
            200, "Gardening Blog", ordinary, has_privacy_frontend=True
        )
        assert signal.is_blocked is False

    def test_login_wall_not_flagged_without_privacy_frontend(self) -> None:
        """The wall flag exists solely to escalate to the
        privacy_frontend tier. On a URL with no frontend fallback
        (resolve_policy returned None) the same wall body must NOT be
        flagged — escalating would only burn the remaining browser
        tiers re-capturing an identical wall. capture_page passes
        has_privacy_frontend=resolve_policy(url) is not None."""
        wall_body = (
            "Don't miss what's happening\n"
            "People on X are the first to know.\n"
            "Log in\nSign up\n"
        )
        signal = check_anti_bot(
            200, "jack on X", wall_body, has_privacy_frontend=False
        )
        assert signal.is_blocked is False

    def test_access_denied_title(self) -> None:
        signal = check_anti_bot(200, "Access Denied", "No access")
        assert signal.is_blocked is True

    def test_case_insensitive_title(self) -> None:
        signal = check_anti_bot(
            200, "CHECKING YOUR BROWSER", "Wait..."
        )
        assert signal.is_blocked is True

    def test_reddit_network_security_block(self) -> None:
        body = (
            "<html><body>You've been blocked by network security. "
            + "If you think you've been blocked by mistake, file a ticket "
            + "below and we'll look into it. " * 10
            + "</body></html>"
        )
        signal = check_anti_bot(200, "Reddit", body)
        assert signal.is_blocked is True
        assert "platform block" in (signal.reason or "")

    def test_swedish_cloudflare_title(self) -> None:
        signal = check_anti_bot(
            200, "Verifiera att du är människa", "challenge"
        )
        assert signal.is_blocked is True
        assert "verifiera" in (signal.reason or "")

    def test_french_cloudflare_title(self) -> None:
        signal = check_anti_bot(
            200, "Un instant...", "challenge"
        )
        assert signal.is_blocked is True

    def test_spanish_cloudflare_title(self) -> None:
        signal = check_anti_bot(
            200, "Un momento, por favor", "challenge"
        )
        assert signal.is_blocked is True

    def test_cloudflare_challenges_url_marker(self) -> None:
        signal = check_anti_bot(
            200,
            "Page",
            "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'>",
        )
        assert signal.is_blocked is True
        assert "Cloudflare" in (signal.reason or "")

    def test_captcha_density_triggers_on_large_shell(self) -> None:
        """WSJ/Reuters returning a 20KB HTML shell full of 'captcha' refs."""
        body = (
            "<html><body>" + ("captcha " * 100) + ("<div>filler</div>" * 200)
            + "</body></html>"
        )
        signal = check_anti_bot(200, "News", body)
        assert signal.is_blocked is True
        assert "density" in (signal.reason or "")

    def test_captcha_passing_mention_does_not_trigger(self) -> None:
        """Article with one incidental 'captcha' mention stays unblocked."""
        body = (
            "<html><body>Today we discuss how captcha technology evolved. "
            + ("Real article content here. " * 500)
            + "</body></html>"
        )
        signal = check_anti_bot(200, "Article", body)
        assert signal.is_blocked is False

    def test_ray_id_marker(self) -> None:
        signal = check_anti_bot(
            200,
            "Blocked",
            "Error 1020. Ray ID: 7c8d9e0f1a2b3c4d",
        )
        # Title contains "blocked" which trips the title check first.
        assert signal.is_blocked is True


class TestDetectJSChallenge:
    def test_anubis_markers_in_body(self) -> None:
        body = (
            '<html><body><div id="app">'
            '<img src="/.within.website/x/cmd/anubis/static/img/pensive.webp"/>'
            '<footer>Protected by Anubis</footer></div></body></html>'
        )
        sig = detect_js_challenge(200, "Making sure you\'re not a bot!", body)
        assert sig is not None
        assert sig.kind == "anubis"

    def test_fingerprintjs_botd_markers(self) -> None:
        body = (
            '<html><head>'
            '<script src="/check/ua-parser.min.js"></script>'
            '<script src="/check/iife.min.js"></script>'
            '<script>var check1 = {"detections": []};</script>'
            '</head></html>'
        )
        sig = detect_js_challenge(503, "", body)
        assert sig is not None
        assert sig.kind == "fingerprintjs_botd"

    def test_cloudflare_jschal_title(self) -> None:
        sig = detect_js_challenge(200, "Just a moment...", "")
        assert sig is not None
        assert sig.kind == "cloudflare_jschal"

    def test_generic_503_with_js_reload(self) -> None:
        body = (
            "<html><head><script>"
            "setTimeout(function(){ window.location.reload() }, 2000);"
            "</script></head></html>"
        )
        sig = detect_js_challenge(503, "Hold on", body)
        assert sig is not None
        assert sig.kind == "generic"

    def test_real_content_not_flagged(self) -> None:
        body = "<html><body>" + ("Normal article content. " * 500) + "</body></html>"
        sig = detect_js_challenge(200, "Great Article", body)
        assert sig is None

    def test_passing_mention_of_anubis_word_not_flagged(self) -> None:
        """A page that mentions Anubis the Egyptian god shouldn't false-positive."""
        body = "<html><body>" + ("The god Anubis was worshipped in ancient Egypt. " * 10) + "</body></html>"
        sig = detect_js_challenge(200, "Egyptian Gods", body)
        # No /.within.website/ or techaro.lol → not flagged
        assert sig is None
