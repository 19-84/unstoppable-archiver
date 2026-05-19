# ABOUTME: Anti-bot detection heuristics for captured pages
# ABOUTME: Inspects HTTP status and page content to detect Cloudflare, CAPTCHAs, and blocks
# pyright: reportUnknownLambdaType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Anti-bot detection heuristics."""

from __future__ import annotations

from dataclasses import dataclass

from beartype import beartype
from icontract import ensure, require

# Cloudflare infrastructure markers — locale-independent, match the iframe
# script URLs / challenge platform hooks. Prefer these over translated copy,
# which breaks for non-English CF challenges (e.g. Swedish wsj.com renders
# "Verifiera enheten" instead of "Just a moment...").
CLOUDFLARE_MARKERS: frozenset[str] = frozenset({
    "cf-browser-verification",
    "cf_chl_opt",
    "cf-turnstile",
    "_cf_chl_tk",
    "challenge-platform",
    "challenges.cloudflare.com",
    "cf-mitigated",
    "__cf_chl_rt_tk",
    "cf-chl-bypass",
    "ray id:",
})

# Platform-specific block pages that return 200 OK with apparently-valid
# content (Reddit especially — their "blocked by network security" page
# was captured as a successful archive until we added this marker).
PLATFORM_BLOCK_MARKERS: frozenset[str] = frozenset({
    "you've been blocked by network security",
    "you have been blocked",
    "sorry, you have been blocked",
    "your ip has been temporarily blocked",
    "unusual activity detected",
    "request could not be satisfied",
    "enable javascript and cookies to continue",
})

# Soft login/content walls. Some sites — notably X/Twitter — serve a
# fully-rendered "log in to see this content" page as HTTP 200 with no
# CAPTCHA and no block language. check_anti_bot's other markers all
# target *hard* blocks (403s, Cloudflare, CAPTCHAs), so a soft wall
# slips through as a "successful" capture: the worker stores 15 MB of
# login-wall HTML and never escalates to the privacy_frontend tier
# (nitter/redlib) that exists precisely to fetch the real content.
#
# Flagging the wall as blocked makes capture_page raise
# AntiBotDetectedError, so the worker escalates through the tiers and
# privacy_frontend serves the actual page. Markers MUST be distinctive
# enough not to appear in legitimate page content — a generic "log in"
# would false-positive on most of the web. These are exact copy from
# the platforms' logged-out walls. The privacy-frontend mirrors
# (nitter etc.) don't carry this copy, so an escalated capture won't
# re-trip the marker.
#
# X/Twitter localizes the wall by the browser's Accept-Language —
# observed live: a stealth-browser tier captured the wall in Swedish
# while a Chromium tier got English. Each marker is therefore the
# "People on X are the first to know" sentence in one observed
# locale. BLOCK_TITLE_PATTERNS below takes the same multilingual
# approach for Cloudflare titles. Locales are added as captures
# surface them — guessing translations risks a non-matching string
# that gives false confidence.
LOGIN_WALL_MARKERS: frozenset[str] = frozenset({
    "people on x are the first to know",       # X/Twitter wall (en)
    "folk på x får reda på allt först",         # X/Twitter wall (sv)
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

# Title patterns spanning English, Spanish, French, German, Portuguese,
# Swedish — Cloudflare and most block pages localize on Accept-Language.
BLOCK_TITLE_PATTERNS: frozenset[str] = frozenset({
    # English
    "access denied",
    "just a moment",
    "attention required",
    "please wait",
    "checking your browser",
    "security check",
    "are you a robot",
    "blocked",
    "forbidden",
    "verify you are human",
    # Spanish (es)
    "un momento",
    "verificando",
    "acceso denegado",
    # French (fr)
    "un instant",
    "accès refusé",
    "vérification",
    # German (de)
    "einen moment",
    "zugriff verweigert",
    "überprüfung",
    # Portuguese (pt) — "verificando" is shared with Spanish, already above
    "um momento",
    "acesso negado",
    # Swedish (sv) — observed in WSJ Cloudflare challenge
    "ett ögonblick",
    "verifiera",
    "verifiering",
})

_BLOCKED_STATUS_CODES = frozenset({403, 429, 503})
_MIN_BODY_LENGTH_FOR_BLOCK = 500
# Captcha-density heuristic: Reuters/WSJ emit 20 KB shells with 80-100
# repetitions of "captcha". Anything >= 10 occurrences per kB of body
# is a strong signal the page is a challenge, not content.
_HIGH_DENSITY_MIN_OCCURRENCES = 10
_HIGH_DENSITY_MIN_PER_KB = 3.0


@dataclass(frozen=True)
class JSChallengeSignal:
    """A JS-challenge page — the document is the challenge, not the target.

    Unlike a block (which we escalate), a challenge is something a real
    browser *solves* by running its JavaScript. Our capture pipeline
    detects these and extends the wait so the browser has a chance to
    complete the challenge before we snapshot.
    """

    kind: str  # "anubis", "fingerprintjs_botd", "cloudflare_jschal", "generic"
    reason: str  # human-readable detection marker


# Markers unique to Anubis (TecharoHQ/anubis) challenge pages. Anubis
# mounts its assets under a `/.within.website/...` base prefix and
# names its cookie `techaro.lol-anubis`. Footer always carries
# "Protected by Anubis" in whatever localized string.
_ANUBIS_MARKERS: tuple[str, ...] = (
    "/.within.website/",
    "techaro.lol-anubis",
    'id="anubis_challenge"',
    'id="anubis_version"',
    'id="preact_info"',
    "github.com/TecharoHQ/anubis",
    "protected by anubis",  # footer text — localized but often English
)

# FingerprintJS BotD-based challenges (xcancel and others using
# Fingerprint's open-source detector via ua-parser-js + iife.min.js).
_FINGERPRINTJS_BOTD_MARKERS: tuple[str, ...] = (
    '/check/ua-parser.min.js',
    '/check/iife.min.js',
    '/check/check2.js',
    'Fingerprint BotD',
    'var check1',  # the inline script's entry variable
    'check1.detections',
)


@beartype
def detect_js_challenge(
    status_code: int, title: str, body_text: str
) -> JSChallengeSignal | None:
    """Detect a solvable JS challenge the browser should be allowed to run.

    Returns a signal identifying the challenge type, or None if this
    isn't a challenge page. Distinct from `check_anti_bot` which flags
    *blocks* — challenges are pages a real browser clears automatically.

    Callers should extend their wait when a signal is returned instead
    of escalating the tier. Escalation should only happen if the
    challenge page is still present after the wait.
    """
    body_lower = body_text.lower()

    if any(m.lower() in body_lower for m in _ANUBIS_MARKERS):
        return JSChallengeSignal(kind="anubis", reason="Anubis marker in body")

    if any(m.lower() in body_lower for m in _FINGERPRINTJS_BOTD_MARKERS):
        return JSChallengeSignal(
            kind="fingerprintjs_botd",
            reason="FingerprintJS BotD / ua-parser challenge",
        )

    # Cloudflare's interactive JS challenge (vs. a final block). Title
    # "Just a moment…" is localized so we also look for structural hints.
    title_lower = title.lower()
    if "just a moment" in title_lower or "cf-chl-opt" in body_lower:
        return JSChallengeSignal(
            kind="cloudflare_jschal", reason="Cloudflare JS interstitial"
        )

    # Generic: 503 with a short body containing script tags + no real
    # content = "hold page + JS will redirect" pattern. Keep narrow —
    # many real pages also fit this loosely.
    if (
        status_code == 503  # noqa: PLR2004
        and len(body_text) < 10000  # noqa: PLR2004
        and "script" in body_lower
        and ("reload" in body_lower or "setTimeout" in body_text)
    ):
        return JSChallengeSignal(
            kind="generic",
            reason="503 + JS reload pattern",
        )

    return None


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
def check_anti_bot(  # noqa: C901, PLR0911, PLR0912
    status_code: int,
    title: str,
    body_text: str,
    *,
    has_privacy_frontend: bool = False,
) -> DetectionSignal:
    """Check if a page response indicates anti-bot blocking.

    Inspects HTTP status, page title, and body text for known
    anti-bot patterns from Cloudflare, CAPTCHAs, and generic blocks.

    `has_privacy_frontend` is True when the captured URL has a
    privacy-frontend fallback (resolve_policy matched). It gates the
    soft login-wall check: that flag exists solely to escalate to the
    privacy_frontend tier, so it is only meaningful when the tier
    exists to serve the real content.
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

    # Platform block pages (Reddit, Akamai, etc.) return 200 OK with
    # fully-rendered block copy. Match regardless of body length.
    for marker in PLATFORM_BLOCK_MARKERS:
        if marker in body_lower:
            return DetectionSignal(
                is_blocked=True,
                reason=f"platform block: '{marker}'",
            )

    # Soft login walls (HTTP 200, no CAPTCHA) — match regardless of
    # body length: X/Twitter's wall ships a multi-MB JS page. Gated on
    # has_privacy_frontend: the flag exists solely to escalate to the
    # privacy_frontend tier, so on a domain with no frontend fallback
    # it would only burn the remaining browser tiers re-capturing the
    # same wall. Gating also bounds false-positive blast radius — a
    # page merely quoting the wall phrase is only ever flagged on an
    # eligible apex (twitter.com/x.com/...), never the open web.
    if has_privacy_frontend:
        for marker in LOGIN_WALL_MARKERS:
            if marker in body_lower:
                return DetectionSignal(
                    is_blocked=True,
                    reason=f"login wall: '{marker}'",
                )

    for marker in GENERIC_BLOCK_MARKERS:
        if marker in body_lower and len(body_text) < _MIN_BODY_LENGTH_FOR_BLOCK:
                return DetectionSignal(
                    is_blocked=True,
                    reason=f"block marker '{marker}' + short body",
                )

    # Density check: a page that is OVERWHELMINGLY captcha boilerplate
    # (e.g. Reuters/WSJ return 20 KB HTML shells with 80+ "captcha"
    # mentions) should be flagged even without the short-body heuristic.
    # The simple < 500-byte check missed those because the shell has
    # enough boilerplate to exceed the threshold.
    density_markers = ("captcha", "recaptcha", "hcaptcha", "turnstile")
    for marker in density_markers:
        count = body_lower.count(marker)
        if count >= _HIGH_DENSITY_MIN_OCCURRENCES and body_text:
            # Require the marker to be at least 1 per ~500 bytes of body
            # so genuine articles that happen to mention "captcha" in
            # passing don't false-positive.
            density = count / max(len(body_text) / 1000, 1)
            if density >= _HIGH_DENSITY_MIN_PER_KB:
                return DetectionSignal(
                    is_blocked=True,
                    reason=(
                        f"captcha density: '{marker}' x{count} "
                        f"in {len(body_text)}B"
                    ),
                )

    return DetectionSignal(is_blocked=False)
