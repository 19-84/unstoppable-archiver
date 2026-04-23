# ABOUTME: Core capture pipeline — one browser session produces all output formats
# ABOUTME: Orchestrates SingleFile (CLI or JS injection), WARC writing, screenshot, and text extraction
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportMissingImports=false
"""Core capture pipeline for archiving web pages."""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
import warnings
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import structlog
from beartype import beartype
from PIL import Image
from playwright.async_api import Browser, BrowserContext, Response, Route
from playwright_stealth import Stealth

from archiver.config import Settings
from archiver.consent import build_consent_init_script
from archiver.cookie_cache import CfClearanceCache
from archiver.darknet import classify_url, get_proxy_for_network
from archiver.detection import check_anti_bot, detect_js_challenge
from archiver.enums import CaptureTier
from archiver.errors import AntiBotDetectedError, CaptureError
from archiver.models import CaptureResult
from archiver.proxy import ProxyConfig
from archiver.singlefile import (
    SINGLEFILE_CAPTURE_JS,
    build_options,
    capture_via_cli,
    load_bundle,
)
from archiver.warc_writer import CapturedExchange, PlaywrightWARCWriter

_CONSENT_CLEANUP_JS = r"""
(() => {
  // Match everything that was hidden by our three injected stylesheets,
  // then yank those elements from the DOM so they're not present in
  // the SingleFile capture at all.
  const tagIds = [
    '__archiver_consent_generic__',
    '__archiver_consent_domain__',
    '__archiver_consent_cmp__',
  ];
  const selectors = new Set();
  for (const id of tagIds) {
    const tag = document.getElementById(id);
    if (!tag) continue;
    try {
      for (const rule of (tag.sheet?.cssRules || [])) {
        if (rule.selectorText) {
          // cssRules joins comma-separated selectors into one string —
          // split so querySelectorAll doesn't fail on an invalid one.
          for (const s of rule.selectorText.split(',')) {
            const trimmed = s.trim();
            if (trimmed) selectors.add(trimmed);
          }
        }
      }
    } catch (_) { /* cross-origin sheet or parse error; skip */ }
  }
  for (const sel of selectors) {
    try {
      document.querySelectorAll(sel).forEach(el => el.remove());
    } catch (_) { /* bad selector; skip */ }
  }
  // Now remove the style tags themselves — they've served their
  // "hide banners during page load" purpose and leaving a 300 KB
  // stylesheet in the DOM blows out SingleFile's rule serializer.
  for (const id of tagIds) {
    const tag = document.getElementById(id);
    if (tag) tag.remove();
  }
})();
"""

# JavaScript-challenge completion probe. Returns true when the page has
# transitioned out of the challenge.
#
# The most reliable signal across challenge types is **title change**:
# - Anubis: "Making sure you're not a bot!" → real page title
# - Cloudflare JS interstitial: "Just a moment..." → real page title
# - FingerprintJS BotD / xcancel: "Attention Required!" / "" → real page title
#
# We fall back to marker-based checks only if title didn't change, in case
# some operator customizes the challenge page title. Cookie-based success
# detection is disabled — auth cookies are typically HttpOnly and invisible
# to document.cookie, which caused false negatives.
_CHALLENGE_TITLE_MARKERS = [
    "making sure you're not a bot",  # Anubis default
    "just a moment",                   # Cloudflare
    "attention required",              # Cloudflare + generic WAFs
    "please enable js",                # FingerprintJS-style
    "checking your browser",           # various
]

_CHALLENGE_COMPLETE_JS = r"""
() => {
  try {
    const title = (document.title || "").toLowerCase();
    const CHALLENGE_TITLES = [
      "making sure you're not a bot",
      "just a moment",
      "attention required",
      "please enable js",
      "checking your browser"
    ];
    // If the document title still matches a challenge, we're not cleared.
    if (CHALLENGE_TITLES.some(m => title.includes(m))) return false;

    // If title changed away from the challenge, we're through.
    // Also require the body to have real content (>5KB) as a sanity check
    // against transient blank states during navigation.
    const bodySize = document.body ? (document.body.innerText || "").length : 0;
    if (bodySize > 256) return true;

    // Positive-signal fallback: look for clearly challenge-specific DOM
    // elements. Only returns "still challenged" if these are present.
    const hasAnubisChallengeScript =
      !!document.getElementById("anubis_challenge") ||
      !!document.getElementById("preact_info");
    const hasFingerprintBotd = typeof (window).check1 === "object";
    const stillChallenged = hasAnubisChallengeScript || hasFingerprintBotd;
    return !stillChallenged;
  } catch (_) {
    return false;
  }
}
"""


_SCROLL_THROUGH_JS = r"""
(() => new Promise(resolve => {
  // Scroll from top to bottom in ~500px steps, pausing briefly at each
  // step so IntersectionObserver-based lazy-load firings have time to
  // resolve their img src. Then scroll back to 0 so the screenshot
  // starts from the real top of the page.
  const step = 500;
  const pauseMs = 120;
  let y = 0;
  const total = Math.max(
    document.documentElement.scrollHeight,
    document.body ? document.body.scrollHeight : 0,
  );
  const timer = setInterval(() => {
    window.scrollTo(0, y);
    y += step;
    if (y >= total) {
      clearInterval(timer);
      window.scrollTo(0, 0);
      resolve();
    }
  }, pauseMs);
  // Safety: cap total scroll time at 30 s.
  setTimeout(() => { clearInterval(timer); window.scrollTo(0, 0); resolve(); }, 30000);
}))()
"""

_stealth = Stealth(
    # Match platform to actual OS to avoid platform/UA mismatch
    navigator_platform_override="Linux x86_64",
    # Consistent WebGL fingerprint (Intel is common and unsuspicious)
    webgl_vendor_override="Intel Inc.",
    webgl_renderer_override="Intel Iris OpenGL Engine",
)

log = structlog.get_logger()


@beartype
async def capture_page(  # noqa: C901, PLR0912, PLR0913, PLR0915
    url: str,
    browser: Browser,
    settings: Settings,
    tier: CaptureTier = CaptureTier.CHROMIUM,
    cookie_cache: CfClearanceCache | None = None,
    strip_selectors: list[str] | None = None,
    proxy: ProxyConfig | None = None,
) -> CaptureResult:
    """Capture a page: SingleFile HTML + WARC + screenshot + text.

    All outputs come from a single browser session. Raises
    AntiBotDetectedError if the page appears to be blocked.
    Raises CaptureError for other failures.

    `strip_selectors`: CSS selectors to remove from the DOM after
    navigation but before SingleFile capture. Used by the Wayback and
    archive.today fallback tiers to hide the host archive's chrome
    (toolbars, banners) so our snapshot records only the original page.
    """
    warc_writer = PlaywrightWARCWriter()
    is_firefox = tier != CaptureTier.CHROMIUM

    # Proxy selection order of precedence:
    #   1. Darknet proxy when URL is onion/i2p (forced by URL).
    #   2. Explicit `proxy` argument (CAMOUFOX_PROXY tier feeds this in).
    # Direct connection otherwise.
    network = classify_url(url)
    effective_proxy = get_proxy_for_network(network, settings)
    if effective_proxy is None and proxy is not None:
        effective_proxy = proxy
        log.info("capture.clearnet_proxy", proxy=proxy.server)
    elif effective_proxy is not None:
        log.info(
            "capture.darknet_proxy",
            network=network.value,
            proxy=effective_proxy.server,
        )
    # Override the browser's default User-Agent. Chromium's default UA
    # on our headless launch includes "HeadlessChrome" which is a
    # trivial bot-detection signal; Camoufox picks a realistic UA via
    # BrowserForge but we override to keep the pool consistent across
    # tiers. Never leak the archiver's identity.
    from archiver import user_agents as _ua
    context_kwargs: dict[str, Any] = {
        "viewport": {"width": 1920, "height": 1080},
        "java_script_enabled": True,
        "user_agent": _ua.pick(),
        # Disable CSP so add_script_tag / add_init_script bundles aren't
        # blocked by strict site headers (smashingmagazine, notion, g2).
        # Without this, injected inline scripts are silently dropped and
        # wait_for_function spins until its 60s timeout.
        "bypass_csp": True,
    }
    if effective_proxy is not None:
        context_kwargs["proxy"] = {"server": effective_proxy.server}

    context = await browser.new_context(**context_kwargs)

    # Strip Content-Security-Policy headers on every response the context
    # fetches. Two reasons:
    #   1. SingleFile's Xray-safe fallback on Camoufox injects an inline
    #      <script> via add_script_tag(content=...). Strict script-src
    #      directives silently drop that injection, making our capture
    #      hang until wait_for_function times out.
    #   2. Our consent-banner CSS injection via add_init_script is also
    #      subject to style-src. Removing CSP makes both paths robust.
    # bypass_csp=True in context_kwargs is a Playwright flag that covers
    # Chromium but Camoufox/Firefox support is incomplete — this response
    # interceptor is the reliable cross-engine fix.
    await context.route("**/*", _strip_csp_route)

    # Apply stealth patches on Chromium contexts to avoid basic detection
    if not is_firefox:
        await _apply_stealth(context)

    # Inject cached cf_clearance cookie if available
    if cookie_cache:
        await _inject_cached_cookies(context, url, cookie_cache)

    page = await context.new_page()

    try:
        # Collect HTTP exchanges for WARC
        async def _on_response(response: Response) -> None:  # pragma: no cover
            try:
                body = await response.body()
                req = response.request
                warc_writer.add_exchange(
                    CapturedExchange(
                        url=response.url,
                        method=req.method,
                        request_headers=dict(
                            await req.all_headers()
                        ),
                        status=response.status,
                        response_headers=dict(
                            await response.all_headers()
                        ),
                        body=body,
                    )
                )
            except Exception:
                log.debug("warc.response_skip", url=response.url)

        page.on("response", _on_response)

        # Inject SingleFile bundle before navigation. The JS-bundle path
        # is the fast path and works on Chromium + most Camoufox pages;
        # the single-file-cli subprocess is the last-resort fallback when
        # Firefox world-isolation makes the in-browser path fail.
        bundle_js = load_bundle(settings.singlefile_bundle_path)
        await page.add_init_script(script=bundle_js)

        # Inject cookie-banner hider BEFORE navigation so rules land
        # pre-paint. Covers both Chromium (no extension support in our
        # headless mode) and Camoufox (uBO ships without annoyances
        # filter group enabled).
        await page.add_init_script(script=build_consent_init_script())

        # Navigate
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.max_capture_timeout * 1000,
            )
        except Exception as nav_exc:
            # Navigation failure — maybe a JS challenge is actively
            # redirecting and never fired DOMContentLoaded on the
            # original doc. Peek at the page, detect challenge,
            # wait for it, then proceed.
            page_title = ""
            body_html = ""
            try:
                page_title = await page.title()
                body_html = await page.evaluate(
                    "document.documentElement"
                    " ? document.documentElement.outerHTML : ''"
                )
            except Exception:
                log.debug("capture.post_timeout_peek_failed")

            challenge = detect_js_challenge(0, page_title, body_html)
            if challenge is not None:
                log.info(
                    "capture.js_challenge_detected_post_timeout",
                    kind=challenge.kind,
                )
                cleared = await _await_challenge_completion(
                    page, timeout_ms=45000
                )
                if cleared:
                    log.info(
                        "capture.js_challenge_cleared_post_timeout",
                        kind=challenge.kind,
                    )
                    response = None  # we don't have a new Response; continue
                else:
                    raise AntiBotDetectedError(
                        f"JS challenge did not clear: {challenge.kind}"
                    ) from nav_exc
            else:
                # Not a challenge — fall back to the prior anti-bot check.
                try:
                    body_text = await page.evaluate(
                        "document.body ? document.body.innerText : ''"
                    )
                    signal = check_anti_bot(0, page_title, body_text)
                    if signal.is_blocked:
                        raise AntiBotDetectedError(
                            f"Blocked (timeout): {signal.reason}"
                        ) from nav_exc
                except AntiBotDetectedError:
                    raise
                except Exception:
                    log.debug("capture.post_timeout_check_failed")
                raise

        # networkidle is best-effort: many SPAs (analytics, websockets,
        # long-polling) never go idle. Proceed with capture on timeout
        # rather than aborting — DOM is already loaded from the step above.
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            log.debug("capture.networkidle_timeout_proceeding")

        status_code = response.status if response else 0
        page_title = await page.title()
        body_text = await page.evaluate(
            "document.body ? document.body.innerText : ''"
        )
        body_html = await page.evaluate(
            "document.documentElement ? document.documentElement.outerHTML : ''"
        )

        # Challenge-aware settle: JS challenges (Anubis PoW, FingerprintJS
        # BotD-based checks) *intend* to be solved by a real browser.
        # Detect known patterns and give the page extra time to complete
        # the challenge before we either capture or escalate.
        challenge = detect_js_challenge(status_code, page_title, body_html)
        if challenge is not None:
            log.info(
                "capture.js_challenge_detected",
                kind=challenge.kind,
                reason=challenge.reason,
            )
            cleared = await _await_challenge_completion(
                page, timeout_ms=45000
            )
            if cleared:
                # Re-read state — the challenge should have navigated or
                # swapped content. Continue with the (now-real) page.
                status_code = 200  # best-effort; we don't have a new Response
                page_title = await page.title()
                body_text = await page.evaluate(
                    "document.body ? document.body.innerText : ''"
                )
                log.info(
                    "capture.js_challenge_cleared", kind=challenge.kind
                )
            else:
                # Challenge didn't clear in time — treat as a block so
                # the tier layer can escalate to a stealthier browser.
                raise AntiBotDetectedError(
                    f"JS challenge did not clear: {challenge.kind}"
                )

        # Check for anti-bot blocking
        signal = check_anti_bot(status_code, page_title, body_text)
        if signal.is_blocked:
            raise AntiBotDetectedError(
                f"Blocked: {signal.reason}"
            )

        # Strip host-archive chrome (Wayback toolbar, archive.today header)
        # before SingleFile so the snapshot contains only the original page.
        if strip_selectors:
            for selector in strip_selectors:
                try:
                    await page.evaluate(
                        "sel => document.querySelectorAll(sel)"
                        ".forEach(e => e.remove())",
                        selector,
                    )
                except Exception:
                    log.debug(
                        "capture.strip_selector_failed", selector=selector
                    )

        # Pre-SingleFile consent cleanup:
        #   1. Physically remove every element that was being hidden by
        #      our injected consent stylesheet — after step 2 removes
        #      the style tag, the elements would otherwise become
        #      visible again in the captured snapshot.
        #   2. Remove the injected <style> tags. Keeping them in the
        #      DOM makes SingleFile's rule serializer blow V8's max
        #      string length ("RangeError: Invalid string length") on
        #      large pages.
        try:
            await page.evaluate(_CONSENT_CLEANUP_JS)
        except Exception:
            log.debug("capture.consent_cleanup_failed")

        # Capture SingleFile snapshot — cascading strategies:
        # 1. JS eval via add_init_script — fastest, works on Chromium
        # 2. Script-tag injection — Firefox fallback for Xray TypedArray
        #    errors; works on Camoufox against non-strict CSP
        # 3. CLI subprocess (single-file-cli) — last-resort on pages where
        #    Firefox's script isolation prevents both in-page strategies
        #    from producing a result. Spawns an independent Chromium that
        #    doesn't share Firefox's world-separation constraints.
        sf_options = build_options(url)
        sf_result: dict[str, Any] | None = None
        try:
            sf_result = await page.evaluate(
                SINGLEFILE_CAPTURE_JS, sf_options
            )
        except Exception as sf_exc:
            if is_firefox and "Xray" in str(sf_exc):
                log.info("capture.singlefile_xray_fallback")
                try:
                    sf_result = await _capture_singlefile_via_script_tag(
                        page, bundle_js, sf_options
                    )
                except Exception as fallback_exc:
                    # Script-tag path is typically blocked by strict CSP;
                    # fall through to the subprocess CLI.
                    log.warning(
                        "capture.singlefile_script_tag_failed",
                        error=str(fallback_exc)[:200],
                    )
                    sf_result = None
            else:
                raise

        # CLI subprocess fallback. Spawns an independent Chromium
        # (which doesn't share Firefox's world-isolation) to snapshot
        # the ORIGINAL URL. Works on strict-CSP sites the Xray path
        # couldn't handle. Fails on sites that fingerprint-gate the
        # CLI's unbranded Chromium (redlib.catsarch.com, cloudflare
        # with strict bot-score requirements).
        if sf_result is None:
            log.info("capture.singlefile_cli_fallback", url=url)
            try:
                snapshot_html_str = await capture_via_cli(
                    url,
                    cli_path=settings.singlefile_cli_path,
                )
                # Sanity: CLI may succeed but produce a block page the
                # target origin served to its unbranded Chromium. If
                # the result looks short + block-ish, fall through to
                # the page.content() path which uses our stealth browser's
                # already-solved session.
                if _looks_like_block_page(snapshot_html_str):
                    log.warning(
                        "capture.singlefile_cli_returned_block",
                        size=len(snapshot_html_str),
                    )
                    sf_result = None
                else:
                    sf_result = {
                        "content": snapshot_html_str,
                        "title": page_title,
                    }
            except Exception as cli_exc:
                log.warning(
                    "capture.singlefile_cli_failed",
                    error=str(cli_exc)[:200],
                )
                sf_result = None

        # Ultimate fallback: grab the live DOM from the current page.
        # Not self-contained (external resources reference originals)
        # but preserves all text/structure — reliably produces a
        # non-empty snapshot when every SingleFile strategy failed.
        if sf_result is None:
            log.info("capture.page_content_fallback")
            try:
                snapshot_html_str = await page.content()
                sf_result = {
                    "content": snapshot_html_str,
                    "title": page_title,
                }
            except Exception as pc_exc:
                raise CaptureError(
                    f"All snapshot strategies failed; last error: {pc_exc}"
                ) from pc_exc

        if not isinstance(sf_result, dict) or "content" not in sf_result:
            raise CaptureError(
                "SingleFile returned unexpected result: "
                + repr(type(sf_result))
            )
        snapshot_html = sf_result["content"].encode("utf-8")
        title = sf_result.get("title") or page_title

        # Screenshot — scroll through the page first so lazy-loaded
        # images are fetched into the DOM before capture. SingleFile's
        # own loadDeferredImages pass doesn't carry over to
        # page.screenshot(full_page=True), so without this the bottom
        # two-thirds of a long page render with blank <img> placeholders.
        try:
            await page.evaluate(_SCROLL_THROUGH_JS)
            # Short settle wait so late-arriving image bytes land before
            # the screenshot serializes.
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            log.debug("capture.pre_screenshot_scroll_failed")
        screenshot_png = await page.screenshot(full_page=True)

        # Text extraction
        text_content: str = await page.evaluate(
            "document.body ? document.body.innerText : ''"
        )

        # Generate thumbnail
        thumbnail_png = _generate_thumbnail(
            screenshot_png,
            settings.thumbnail_width,
            settings.thumbnail_height,
        )

        # Write WARC
        warc_path = None
        warc_size = 0
        if warc_writer.exchange_count > 0:
            warc_path = (
                settings.artifacts_dir / f"tmp_{uuid.uuid4().hex}.warc.gz"
            )
            warc_size = warc_writer.finalize(warc_path)

        # Content hashes for dedup
        content_hash = hashlib.sha256(snapshot_html).hexdigest()
        screenshot_hash = hashlib.sha256(
            screenshot_png
        ).hexdigest()

        # Cache cf_clearance cookie if present
        if cookie_cache:
            await _cache_cf_clearance(context, url, cookie_cache)

        return CaptureResult(
            snapshot_html=snapshot_html,
            screenshot_png=screenshot_png,
            thumbnail_png=thumbnail_png,
            text_content=text_content,
            title=title,
            warc_path=warc_path,
            warc_size=warc_size,
            content_hash=content_hash,
            screenshot_hash=screenshot_hash,
        )

    except AntiBotDetectedError:
        raise
    except Exception as exc:
        raise CaptureError(f"Capture failed: {exc}") from exc
    finally:
        await context.close()


_CSP_HEADER_NAMES: frozenset[str] = frozenset({
    "content-security-policy",
    "content-security-policy-report-only",
    "x-content-security-policy",  # legacy (pre-standard) MSIE/Firefox
    "x-webkit-csp",               # legacy Safari/Chrome prefixed
})


async def _strip_csp_route(route: Route) -> None:
    """Strip Content-Security-Policy headers from every response.

    Intercepts the request via Playwright's routing, fetches upstream,
    and replays the response to the browser with CSP headers removed.
    Lets SingleFile's inline-script and style-tag injections work on
    sites with strict `script-src`/`style-src` policies (redlib, notion,
    smashingmagazine, GitHub, etc.).

    The resulting snapshot is unaffected — SingleFile embeds everything
    inline, and the viewer runs the captured HTML in a sandboxed iframe
    (sandbox="" — no scripts), so removing CSP at capture time does not
    relax any security property of the archive.

    Non-HTTP(S) schemes (data:, blob:) can't be fetched via route.fetch;
    those continue unmodified. Any error during interception also falls
    back to continue() so we never strand a request.
    """
    try:
        if not route.request.url.lower().startswith(("http://", "https://")):
            await route.continue_()
            return
        response = await route.fetch()
        headers = {
            k: v for k, v in response.headers.items()
            if k.lower() not in _CSP_HEADER_NAMES
        }
        await route.fulfill(response=response, headers=headers)
    except Exception as exc:
        # Route handlers must not raise — unfulfilled routes hang the
        # browser. Fall back to an unmodified pass-through.
        log.debug(
            "capture.csp_strip_route_failed",
            url=route.request.url,
            error=str(exc)[:120],
        )
        with contextlib.suppress(Exception):
            await route.continue_()


def _looks_like_block_page(html: str) -> bool:
    """Heuristic: does a CLI-returned HTML look like a block page?

    The CLI's unbranded Chromium gets fingerprint-blocked at ingress
    by some origins (datacenter IP + vanilla UA). Block pages can
    range 5-100 KB (static CSS inlined), so size alone isn't a
    signal — we look for strong title markers.

    Checked in the first 4 KB to avoid false positives on long articles
    that might mention "Forbidden" in their body text.
    """
    head = html[:4096].lower()
    markers = (
        "<title>403",
        "<title>access denied",
        "<title>forbidden",
        "<title>blocked",
        "403 forbidden",
    )
    return any(m in head for m in markers)


async def _await_challenge_completion(
    page: Any, timeout_ms: int = 45000
) -> bool:
    """Wait for a JS challenge page to transition to real content.

    Polls `_CHALLENGE_COMPLETE_JS` inside the page until it returns
    true, or until `timeout_ms` elapses. Returns True on success,
    False on timeout. Safe to call on any page — returns quickly when
    no challenge is active.

    After this returns True, the caller should re-read page state
    (title, body) since the challenge likely navigated or replaced
    the document.
    """
    try:
        await page.wait_for_function(
            _CHALLENGE_COMPLETE_JS, timeout=timeout_ms
        )
        # Small settle so any post-challenge XHRs / redirects finish.
        # networkidle is best-effort — some sites never actually idle.
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)
        return True
    except Exception as exc:
        log.warning(
            "capture.challenge_wait_timeout", error=str(exc)[:200]
        )
        return False


@beartype
async def _capture_singlefile_via_script_tag(
    page: Any,
    bundle_js: str,
    sf_options: dict[str, Any],
) -> dict[str, str]:
    """Run SingleFile capture entirely in the page's DOM context.

    Injects the bundle + capture call as a <script> tag so all code
    runs without Firefox Xray privilege boundaries.
    """
    options_json = json.dumps(sf_options)
    capture_script = (
        bundle_js
        + "\n;(async () => {"
        + f"  const opts = {options_json};"
        + "  const r = await singlefile.getPageData(opts);"
        + "  window.__sf_result = { content: r.content, title: r.title };"
        + "})().catch(e => { window.__sf_result = { error: String(e) }; });"
    )
    await page.add_script_tag(content=capture_script)
    # 15s timeout: strict-CSP sites never populate __sf_result (inline
    # script blocked). When this fails, the caller falls through to the
    # CLI subprocess — spending 60s per tier only to bail was wasteful.
    # Successful captures on non-CSP pages typically finish in 3-8 s.
    await page.wait_for_function(
        "window.__sf_result !== undefined", timeout=15000
    )
    result: dict[str, str] = await page.evaluate("window.__sf_result")
    if "error" in result:
        raise CaptureError(f"SingleFile (script tag): {result['error']}")
    return result


async def _apply_stealth(context: BrowserContext) -> None:
    """Apply playwright-stealth patches to a browser context."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        await _stealth.apply_stealth_async(context)
    log.debug("stealth.applied")


async def _inject_cached_cookies(
    context: BrowserContext,
    url: str,
    cache: CfClearanceCache,
) -> None:
    """Inject cached cf_clearance cookie into browser context."""
    cookie = cache.get_for_url(url)
    if cookie:
        await context.add_cookies([{
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "httpOnly": True,
            "secure": True,
        }])
        log.debug("cookie_cache.injected", domain=cookie.domain)


async def _cache_cf_clearance(
    context: BrowserContext,
    url: str,
    cache: CfClearanceCache,
) -> None:
    """Extract and cache cf_clearance cookie from browser context."""
    try:
        cookies = await context.cookies()
        for c in cookies:
            if c.get("name") == "cf_clearance":
                domain = c.get("domain", urlparse(url).hostname or "")
                cache.put(
                    domain=domain,
                    name=c["name"],
                    value=c["value"],
                    path=c.get("path", "/"),
                )
    except Exception:
        log.debug("cookie_cache.extract_failed")


@beartype
def _generate_thumbnail(
    screenshot_png: bytes,
    width: int,
    height: int,
) -> bytes:
    """Resize screenshot to thumbnail dimensions."""
    img = Image.open(BytesIO(screenshot_png))
    img.thumbnail((width, height), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@beartype
async def save_artifacts(
    result: CaptureResult,
    url_hash: str,
    artifacts_dir: Path,
) -> str:
    """Save all capture artifacts to disk.

    Returns the relative artifact directory path.
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    rel_dir = f"{url_hash}/{timestamp}"
    out_dir = artifacts_dir / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "snapshot.html").write_bytes(result.snapshot_html)
    (out_dir / "screenshot.png").write_bytes(result.screenshot_png)
    (out_dir / "thumbnail.png").write_bytes(result.thumbnail_png)

    if result.warc_path and result.warc_path.exists():
        result.warc_path.rename(out_dir / "archive.warc.gz")

    log.info(
        "artifacts.saved",
        dir=rel_dir,
        snapshot_size=len(result.snapshot_html),
        warc_size=result.warc_size,
    )
    return rel_dir
