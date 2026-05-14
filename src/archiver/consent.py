# ABOUTME: Cookie/consent banner dismissal injected into every capture
# ABOUTME: Loads vendored Easylist Cookie rules, injects CSS + domain-matching JS
"""Cross-browser cookie-banner hiding for the capture pipeline.

uBlock Origin is bundled with Camoufox but the Annoyances filter group
(which catches cookie banners) is off by default, and uBO doesn't run in
Chromium at all. This module packages the same rules Easylist Cookie
publishes and makes them work on both engines via pure DOM/CSS injection.

Exposes `build_consent_init_script()` — a single JS string suitable for
`page.add_init_script(script=...)`. The script:
  1. Inserts a <style> with the generic hide rules before first paint.
  2. On DOMContentLoaded, adds domain-specific rules matching hostname.
  3. Removes common body/html scroll-lock classes that linger after the
     banner itself is hidden (blocks page interaction otherwise).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import structlog
from beartype import beartype

log = structlog.get_logger()

_VENDOR_DIR = Path(__file__).parent / "vendor"
_CSS_PATH = _VENDOR_DIR / "cookie_filters.css"
_JSON_PATH = _VENDOR_DIR / "cookie_filters.json"

# Selectors for major Consent Management Platforms (CMPs) that
# Easylist Cookie misses because they're iframe-based and inject
# randomized IDs at runtime. Each entry targets the wrapper element —
# removing it takes the modal + any backdrop with it.
_CMP_SELECTORS: tuple[str, ...] = (
    # Sourcepoint (Guardian, Bloomberg, Reuters, WSJ, etc.)
    'iframe[id^="sp_message_iframe_"]',
    'div[id^="sp_message_container_"]',
    'div.sp_veil',
    'div.sp-message-open',
    # OneTrust (widely used by Fortune 500)
    "#onetrust-consent-sdk",
    "#onetrust-banner-sdk",
    "#onetrust-pc-sdk",
    ".onetrust-pc-dark-filter",
    ".ot-sdk-container",
    # Didomi
    "#didomi-host",
    "#didomi-notice",
    ".didomi-popup-container",
    ".didomi-popup-view",
    # Quantcast Choice (formerly Quantcast Consent)
    ".qc-cmp2-container",
    ".qc-cmp2-bg",
    "#qc-cmp2-ui",
    # CookieBot
    "#CookiebotBanner",
    "#CybotCookiebotDialog",
    "#CybotCookiebotDialogBodyUnderlay",
    # TrustArc
    "#truste-consent-track",
    "#truste_popframe",
    "#truste_popup_overlay",
    # Usercentrics
    "#usercentrics-root",
    "div[class*='uc-banner']",
    # Klaro
    "#klaro",
    # Cookie-first / Iubenda / Secure Privacy
    "#cookie-first-banner",
    "#iubenda-cs-banner",
    "#sp-cc",
    # Generic "this wrapper is clearly consent" attribute patterns.
    # Kept conservative to avoid stripping real UI — only match
    # elements whose attribute clearly names them as consent.
    '[data-testid*="consent-banner"]',
    '[data-testid*="cookie-banner"]',
    '[data-test-id*="consent-dialog"]',
    '[aria-label*="Cookie banner"]',
    '[aria-label*="Consent banner"]',
)


@lru_cache(maxsize=1)
def _load_css() -> str:
    if not _CSS_PATH.exists():
        log.warning("consent.css_missing", path=str(_CSS_PATH))
        return ""
    return _CSS_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _load_domain_rules() -> dict[str, dict[str, list[str]]]:
    if not _JSON_PATH.exists():
        log.warning("consent.json_missing", path=str(_JSON_PATH))
        return {"rules": {}, "exceptions": {}}
    return json.loads(_JSON_PATH.read_text(encoding="utf-8"))


@beartype
def build_consent_init_script() -> str:
    """Return a JS payload that hides consent banners on the page.

    The script is fully self-contained — it inlines the CSS and domain
    rules rather than fetching at runtime, so the capture has no network
    dependency for filter loading.
    """
    css = _load_css()
    domain_payload = _load_domain_rules()
    if not css and not domain_payload.get("rules"):
        return "/* consent: no filter data available */"

    # Encoding: pass both blobs as JSON-safe strings so the script can
    # be injected verbatim regardless of the selectors' contents.
    css_b = json.dumps(css)
    rules_b = json.dumps(domain_payload.get("rules", {}))
    exceptions_b = json.dumps(domain_payload.get("exceptions", {}))
    cmp_b = json.dumps(list(_CMP_SELECTORS))

    return _INJECTOR_TEMPLATE.format(
        css_blob=css_b,
        rules_blob=rules_b,
        exceptions_blob=exceptions_b,
        cmp_blob=cmp_b,
    )


# JavaScript template. Uses `{{` and `}}` as literal braces because
# .format() unescapes them. Placeholders: css_blob, rules_blob, exceptions_blob.
_INJECTOR_TEMPLATE = r"""
(() => {{
  const CSS = {css_blob};
  const DOMAIN_RULES = {rules_blob};
  const EXCEPTIONS = {exceptions_blob};
  const CMP_SELECTORS = {cmp_blob};

  // --- Generic CSS: insert synchronously so rules apply pre-paint. ---
  const insertStyle = (textContent, id) => {{
    try {{
      const existing = document.getElementById(id);
      if (existing) return;
      const style = document.createElement("style");
      style.id = id;
      style.textContent = textContent;
      (document.head || document.documentElement).appendChild(style);
    }} catch (_) {{ /* pre-DOM insert can race; ignore */ }}
  }};

  // If the document head is already available, attach immediately.
  // Otherwise retry on readystatechange — add_init_script runs very
  // early, often before document.documentElement exists.
  const tryInsertGeneric = () => insertStyle(CSS, "__archiver_consent_generic__");
  tryInsertGeneric();

  // Additional CMP-specific rules: Sourcepoint, OneTrust, Didomi,
  // Quantcast, CookieBot, TrustArc, Usercentrics, etc. Easylist Cookie
  // misses these because they're iframe-mounted with random IDs.
  if (CMP_SELECTORS.length) {{
    const cmpCss = CMP_SELECTORS.join(",") + " {{ display: none !important; }}";
    insertStyle(cmpCss, "__archiver_consent_cmp__");
  }}
  if (!document.head && !document.documentElement) {{
    document.addEventListener("readystatechange", tryInsertGeneric, {{ once: true }});
  }}

  // --- Domain-specific rules. ---
  const applyDomainRules = () => {{
    const host = (location.hostname || "").toLowerCase();
    if (!host) return;

    // Find the most-specific matching domain key — walk up the
    // subdomain chain so "www.theguardian.com" picks up rules for
    // "theguardian.com".
    const candidates = [];
    const parts = host.split(".");
    for (let i = 0; i < parts.length - 1; i++) {{
      candidates.push(parts.slice(i).join("."));
    }}

    const selectors = new Set();
    const suppressed = new Set();
    for (const key of candidates) {{
      if (DOMAIN_RULES[key]) for (const s of DOMAIN_RULES[key]) selectors.add(s);
      if (EXCEPTIONS[key]) for (const s of EXCEPTIONS[key]) suppressed.add(s);
    }}
    for (const s of suppressed) selectors.delete(s);
    if (!selectors.size) return;

    const combined = [...selectors].join(",");
    insertStyle(
      combined + " {{ display: none !important; }}",
      "__archiver_consent_domain__"
    );
  }};

  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", applyDomainRules, {{ once: true }});
  }} else {{
    applyDomainRules();
  }}

  // Actively remove CMP elements as they're injected. CMPs often mount
  // their containers AFTER DOMContentLoaded, so a MutationObserver
  // catches them at insertion time instead of relying on CSS alone.
  const cmpCleaner = () => {{
    for (const sel of CMP_SELECTORS) {{
      try {{
        document.querySelectorAll(sel).forEach(el => el.remove());
      }} catch (_) {{ /* invalid selector for this browser; skip */ }}
    }}
  }};
  cmpCleaner();
  try {{
    const mo = new MutationObserver(() => cmpCleaner());
    const startObserver = () => {{
      if (document.body) mo.observe(document.body, {{ childList: true, subtree: true }});
      else document.addEventListener("DOMContentLoaded", startObserver, {{ once: true }});
    }};
    startObserver();
    // Stop observing after 10 s — CMPs mount within seconds; beyond
    // that we're only adding overhead to long-running pages.
    setTimeout(() => mo.disconnect(), 10000);
  }} catch (_) {{ /* no MutationObserver support; cleanup at capture-time covers it */ }}

  // --- Scroll-lock cleanup. Consent modals often lock body scroll via
  // class or inline style; hiding the modal leaves the page unscrollable.
  // Remove the common lock patterns so SingleFile can snapshot the page
  // normally after banners disappear. Runs repeatedly for ~2 s because
  // some platforms reapply the lock asynchronously. ---
  const LOCK_CLASSES = [
    "noscroll", "no-scroll", "scroll-lock", "modal-open", "cookie-open",
    "u-preventScroll", "overflow-hidden", "is-locked", "body--is-locked",
  ];
  const clearLocks = () => {{
    try {{
      const body = document.body, html = document.documentElement;
      if (!body) return;
      for (const cls of LOCK_CLASSES) {{
        body.classList.remove(cls);
        if (html) html.classList.remove(cls);
      }}
      // Common inline style lock: `overflow: hidden`.
      if (body.style.overflow === "hidden") body.style.overflow = "";
      if (html && html.style.overflow === "hidden") html.style.overflow = "";
    }} catch (_) {{ /* ignore */ }}
  }};
  clearLocks();
  let lockPolls = 0;
  const lockInterval = setInterval(() => {{
    clearLocks();
    if (++lockPolls >= 20) clearInterval(lockInterval);
  }}, 100);
}})();
"""
