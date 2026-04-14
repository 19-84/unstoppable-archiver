# ABOUTME: Anti-bot detection heuristics for captured pages
# ABOUTME: Inspects HTTP status and page content to detect Cloudflare, CAPTCHAs, and blocks
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Anti-bot detection heuristics."""

from __future__ import annotations

from dataclasses import dataclass

from beartype import beartype
from icontract import ensure, require

CLOUDFLARE_MARKERS: frozenset[str] = frozenset({
    "cf-browser-verification",
    "cf_chl_opt",
    "cf-turnstile",
    "_cf_chl_tk",
    "challenge-platform",
})

GENERIC_BLOCK_MARKERS: frozenset[str] = frozenset({
    "captcha",
    "recaptcha",
    "hcaptcha",
    "bot detection",
    "automated access",
    "please verify",
    "unusual traffic",
})

BLOCK_TITLE_PATTERNS: frozenset[str] = frozenset({
    "access denied",
    "just a moment",
    "attention required",
    "please wait",
    "checking your browser",
    "security check",
    "are you a robot",
    "blocked",
    "forbidden",
})

_BLOCKED_STATUS_CODES = frozenset({403, 429, 503})
_MIN_BODY_LENGTH_FOR_BLOCK = 500


@dataclass(frozen=True)
class DetectionSignal:
    """Result of anti-bot detection check."""

    is_blocked: bool
    reason: str | None = None


@beartype
@require(lambda status_code: status_code >= 0, "Status code must be non-negative")
@ensure(
    lambda result: result.reason is not None if result.is_blocked else result.reason is None,
    "Blocked signals must have a reason; non-blocked must not",
)
def check_anti_bot(
    status_code: int,
    title: str,
    body_text: str,
) -> DetectionSignal:
    """Check if a page response indicates anti-bot blocking.

    Inspects HTTP status, page title, and body text for known
    anti-bot patterns from Cloudflare, CAPTCHAs, and generic blocks.
    """
    if status_code in _BLOCKED_STATUS_CODES:
        return DetectionSignal(
            is_blocked=True,
            reason=f"HTTP {status_code}",
        )

    title_lower = title.lower()
    for pattern in BLOCK_TITLE_PATTERNS:
        if pattern in title_lower:
            return DetectionSignal(
                is_blocked=True,
                reason=f"title contains '{pattern}'",
            )

    body_lower = body_text.lower()
    for marker in CLOUDFLARE_MARKERS:
        if marker in body_lower:
            return DetectionSignal(
                is_blocked=True,
                reason=f"Cloudflare marker: {marker}",
            )

    for marker in GENERIC_BLOCK_MARKERS:
        if marker in body_lower and len(body_text) < _MIN_BODY_LENGTH_FOR_BLOCK:
                return DetectionSignal(
                    is_blocked=True,
                    reason=f"block marker '{marker}' + short body",
                )

    return DetectionSignal(is_blocked=False)
