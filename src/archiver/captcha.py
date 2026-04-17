# ABOUTME: Captcha verification — hCaptcha (third-party) or Altcha (self-hosted PoW)
# ABOUTME: Provider-agnostic verify() dispatches to the configured backend
"""Captcha verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any

import httpx
import structlog
from beartype import beartype

from archiver.config import Settings

log = structlog.get_logger()

_HCAPTCHA_VERIFY_URL = "https://hcaptcha.com/siteverify"


@beartype
async def verify(settings: Settings, token: str) -> bool:
    """Verify a captcha response token. Provider-agnostic.

    Returns True if captcha is disabled or the token is valid.
    Returns False on verification failure.
    """
    provider = settings.captcha_provider
    if provider == "none":
        return True
    if not token:
        return False
    if provider == "hcaptcha":
        return await _verify_hcaptcha(
            token, settings.hcaptcha_secret.get_secret_value()
        )
    if provider == "altcha":
        return _verify_altcha(
            token, settings.altcha_hmac_key.get_secret_value()
        )
    return False


@beartype
async def _verify_hcaptcha(token: str, secret: str) -> bool:
    """Verify an hCaptcha response against hcaptcha.com."""
    if not secret:
        log.warning("captcha.hcaptcha.missing_secret")
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                _HCAPTCHA_VERIFY_URL,
                data={"secret": secret, "response": token},
            )
            data = resp.json()
            return bool(data.get("success", False))
    except Exception as exc:
        log.warning("captcha.hcaptcha.verify_error", error=str(exc))
        return False


@beartype
def generate_altcha_challenge(
    hmac_key: str, max_number: int = 50000
) -> dict[str, Any]:
    """Generate an Altcha proof-of-work challenge.

    Returns a dict the client JS consumes:
      algorithm, challenge, maxnumber, salt, signature
    """
    if not hmac_key:
        raise ValueError("altcha_hmac_key not configured")
    salt = secrets.token_hex(12)
    secret_number = secrets.randbelow(max_number)
    challenge = hashlib.sha256(
        (salt + str(secret_number)).encode()
    ).hexdigest()
    signature = hmac.new(
        hmac_key.encode(),
        challenge.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "algorithm": "SHA-256",
        "challenge": challenge,
        "maxnumber": max_number,
        "salt": salt,
        "signature": signature,
    }


@beartype
def _verify_altcha(token: str, hmac_key: str) -> bool:  # noqa: PLR0911
    """Verify an Altcha proof-of-work response.

    Token is base64-encoded JSON: {algorithm, challenge, number, salt, signature}
    """
    if not hmac_key:
        log.warning("captcha.altcha.missing_hmac_key")
        return False
    try:
        decoded = base64.b64decode(token).decode("utf-8")
        payload = json.loads(decoded)
    except Exception as exc:
        log.warning("captcha.altcha.decode_error", error=str(exc))
        return False

    algorithm = payload.get("algorithm", "")
    challenge = payload.get("challenge", "")
    number = payload.get("number")
    salt = payload.get("salt", "")
    signature = payload.get("signature", "")

    if algorithm != "SHA-256":
        return False
    if not isinstance(number, int):
        return False

    # Verify PoW: sha256(salt + number) must match challenge
    expected_challenge = hashlib.sha256(
        (salt + str(number)).encode()
    ).hexdigest()
    if not hmac.compare_digest(expected_challenge, challenge):
        return False

    # Verify HMAC signature of the challenge
    expected_sig = hmac.new(
        hmac_key.encode(),
        challenge.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_sig, signature)
