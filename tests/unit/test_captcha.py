# ABOUTME: Tests for captcha verification — hcaptcha + altcha providers
# ABOUTME: Covers provider dispatch, PoW verification, HMAC signature validation
"""Tests for captcha module."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import respx

from archiver.captcha import (
    _verify_altcha,
    generate_altcha_challenge,
    verify,
)
from archiver.config import Settings


class TestVerifyNoneProvider:
    async def test_no_captcha_always_passes(self) -> None:
        settings = Settings(captcha_provider="none")
        assert await verify(settings, "") is True
        assert await verify(settings, "anything") is True


class TestVerifyHcaptcha:
    @respx.mock
    async def test_success(self) -> None:
        respx.post("https://hcaptcha.com/siteverify").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        settings = Settings(
            captcha_provider="hcaptcha",
            hcaptcha_secret="test-secret",  # type: ignore[arg-type]
        )
        assert await verify(settings, "good-token") is True

    @respx.mock
    async def test_failure(self) -> None:
        respx.post("https://hcaptcha.com/siteverify").mock(
            return_value=httpx.Response(200, json={"success": False})
        )
        settings = Settings(
            captcha_provider="hcaptcha",
            hcaptcha_secret="test-secret",  # type: ignore[arg-type]
        )
        assert await verify(settings, "bad-token") is False

    async def test_empty_token_fails(self) -> None:
        settings = Settings(captcha_provider="hcaptcha")
        assert await verify(settings, "") is False

    async def test_missing_secret_fails(self) -> None:
        settings = Settings(captcha_provider="hcaptcha")
        assert await verify(settings, "token") is False


class TestAltchaChallenge:
    def test_generates_valid_challenge(self) -> None:
        c = generate_altcha_challenge("test-key", max_number=100)
        assert c["algorithm"] == "SHA-256"
        assert len(c["challenge"]) == 64  # sha256 hex  # noqa: PLR2004
        assert len(c["signature"]) == 64  # noqa: PLR2004
        assert c["maxnumber"] == 100  # noqa: PLR2004

    def test_signature_matches(self) -> None:
        c = generate_altcha_challenge("test-key")
        expected = hmac.new(
            b"test-key", c["challenge"].encode(), hashlib.sha256
        ).hexdigest()
        assert c["signature"] == expected


def _solve_altcha(challenge: dict) -> str:  # type: ignore[type-arg]
    """Solve an altcha challenge and return the base64-encoded token."""
    salt = challenge["salt"]
    target = challenge["challenge"]
    for n in range(challenge["maxnumber"] + 1):
        if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() == target:
            payload = {
                "algorithm": "SHA-256",
                "challenge": target,
                "number": n,
                "salt": salt,
                "signature": challenge["signature"],
            }
            return base64.b64encode(json.dumps(payload).encode()).decode()
    raise RuntimeError("unsolved")


class TestVerifyAltcha:
    def test_valid_solution(self) -> None:
        challenge = generate_altcha_challenge("hmac-key", max_number=100)
        token = _solve_altcha(challenge)
        assert _verify_altcha(token, "hmac-key") is True

    def test_tampered_number_fails(self) -> None:
        challenge = generate_altcha_challenge("hmac-key", max_number=100)
        token = _solve_altcha(challenge)
        payload = json.loads(base64.b64decode(token))
        payload["number"] = payload["number"] + 1
        bad_token = base64.b64encode(json.dumps(payload).encode()).decode()
        assert _verify_altcha(bad_token, "hmac-key") is False

    def test_wrong_hmac_key_fails(self) -> None:
        challenge = generate_altcha_challenge("hmac-key", max_number=100)
        token = _solve_altcha(challenge)
        assert _verify_altcha(token, "different-key") is False

    def test_malformed_token_fails(self) -> None:
        assert _verify_altcha("not-base64!", "key") is False
        assert _verify_altcha("", "key") is False

    def test_missing_key_fails(self) -> None:
        challenge = generate_altcha_challenge("hmac-key", max_number=10)
        token = _solve_altcha(challenge)
        assert _verify_altcha(token, "") is False
