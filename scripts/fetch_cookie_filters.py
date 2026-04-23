#!/usr/bin/env python3
# ABOUTME: Build-time fetcher that parses Easylist Cookie rules into CSS + JSON
# ABOUTME: Run: uv run python scripts/fetch_cookie_filters.py (or via CI)
"""Fetch Easylist Cookie, emit vendored assets for runtime injection.

The capture pipeline injects these into every page to hide cookie/consent
banners across browsers — uBlock Origin handles it in Camoufox only, and
even there the Annoyances list is off by default.

Outputs:
  src/archiver/vendor/cookie_filters.css      Generic element-hide rules
  src/archiver/vendor/cookie_filters.json     Domain-scoped + exceptions

The list is LGPL/CC-BY licensed — safe to bundle. See
https://easylist.to/ for the authoritative source.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import httpx

SOURCE_URL = "https://secure.fanboy.co.nz/fanboy-cookiemonster.txt"
VENDOR_DIR = Path(__file__).resolve().parent.parent / "src/archiver/vendor"
CSS_OUTPUT = VENDOR_DIR / "cookie_filters.css"
JSON_OUTPUT = VENDOR_DIR / "cookie_filters.json"

# ABP element-hide rule: [domains]##selector
# Domains comma-separated, "~" prefix = exclusion
# `#@#` = element-hide exception ("don't hide on this domain")
_HIDE_RE = re.compile(
    r"^(?P<domains>[a-zA-Z0-9.\-,~]*)(?P<op>#@?#)(?P<selector>.+)$"
)


def parse_rules(body: str) -> tuple[list[str], dict[str, list[str]], dict[str, list[str]]]:
    """Return (generic_selectors, domain_rules, domain_exceptions).

    - generic_selectors: apply everywhere (domains field empty)
    - domain_rules: {hostname: [selectors]} — apply only on that hostname
    - domain_exceptions: {hostname: [selectors]} — don't hide on that hostname
    """
    generic: list[str] = []
    by_domain: dict[str, list[str]] = defaultdict(list)
    exceptions: dict[str, list[str]] = defaultdict(list)

    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("!") or line.startswith("["):
            continue
        match = _HIDE_RE.match(line)
        if not match:
            # Network-blocking rule or unsupported — skip.
            continue
        selector = match.group("selector").strip()
        if not selector or _contains_abp_extension(selector):
            continue
        op = match.group("op")
        domains_field = match.group("domains")
        is_exception = op == "#@#"

        if not domains_field:
            # Generic rule — exceptions without a domain field are
            # network-scope exceptions, not per-site. Skip those.
            if not is_exception:
                generic.append(selector)
            continue

        for domain_token in domains_field.split(","):
            domain = domain_token.strip()
            if not domain or domain.startswith("~"):
                continue
            target = exceptions if is_exception else by_domain
            target[domain.lower()].append(selector)

    return generic, dict(by_domain), dict(exceptions)


def _contains_abp_extension(selector: str) -> bool:
    """Reject selectors that use ABP-specific extensions we don't implement."""
    # :has-text, :matches-css, :abp-has, etc. — Chrome CSS doesn't support.
    banned = (
        ":has-text(",
        ":matches-css(",
        ":abp-",
        ":-abp-",
        ":if(",
        ":if-not(",
        ":properties(",
        ":style(",
        ":xpath(",
        ":watch-attr(",
        ":contains(",
        ":upward(",
        ":min-text-length(",
    )
    return any(b in selector for b in banned)


def build_css(selectors: list[str]) -> str:
    """Emit a single CSS rule hiding all generic selectors with !important.

    Comma-joining one giant ruleset is more parse-efficient than thousands
    of separate rulesets, and the browser handles 15 k selectors fine.
    """
    # De-duplicate while preserving the order.
    seen: set[str] = set()
    uniq: list[str] = []
    for s in selectors:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    # Guard against selectors breaking overall parse: a single malformed
    # selector invalidates the entire rule. Emit in chunks so one bad
    # selector only nukes its chunk.
    chunk_size = 250
    parts: list[str] = []
    for i in range(0, len(uniq), chunk_size):
        joined = ",\n".join(uniq[i : i + chunk_size])
        parts.append(f"{joined} {{ display: none !important; }}")
    return "\n".join(parts)


def main() -> int:
    print(f"fetching {SOURCE_URL}", file=sys.stderr)
    resp = httpx.get(SOURCE_URL, timeout=30)
    resp.raise_for_status()
    body = resp.text

    generic, by_domain, exceptions = parse_rules(body)
    print(
        f"parsed: {len(generic)} generic, "
        f"{len(by_domain)} domains with rules, "
        f"{len(exceptions)} domains with exceptions",
        file=sys.stderr,
    )

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)

    css = build_css(generic)
    CSS_OUTPUT.write_text(css, encoding="utf-8")
    print(f"wrote {CSS_OUTPUT} ({len(css):,} bytes)", file=sys.stderr)

    domain_payload = {
        "rules": {k: sorted(set(v)) for k, v in by_domain.items()},
        "exceptions": {k: sorted(set(v)) for k, v in exceptions.items()},
    }
    JSON_OUTPUT.write_text(
        json.dumps(domain_payload, separators=(",", ":")), encoding="utf-8"
    )
    print(
        f"wrote {JSON_OUTPUT} "
        f"({JSON_OUTPUT.stat().st_size:,} bytes)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
