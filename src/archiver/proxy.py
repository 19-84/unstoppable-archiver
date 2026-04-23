# ABOUTME: Proxy list management with round-robin rotation and health tracking
# ABOUTME: Provides proxy configs for Tier 3 (custom proxies), Tor, and I2P capture
# pyright: reportUnknownVariableType=false, reportUnknownLambdaType=false, reportUnknownArgumentType=false
"""Proxy rotation for capture tiers."""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import structlog
from beartype import beartype
from icontract import require

log = structlog.get_logger()

# Schemes we accept in proxy entries. httpx / Playwright support all four;
# Camoufox uses its embedded Firefox which handles http(s) and socks4/5.
_VALID_SCHEMES: frozenset[str] = frozenset(
    {"http", "https", "socks4", "socks5"}
)


@dataclass(frozen=True)
class ProxyConfig:
    """A single proxy endpoint."""

    server: str  # protocol://host:port


@dataclass
class ProxyRotator:
    """Round-robin proxy selection with failure tracking."""

    proxies: list[ProxyConfig] = field(default_factory=list)
    _failed: set[str] = field(default_factory=set, repr=False)
    _cycle: itertools.cycle[ProxyConfig] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        if self.proxies:
            self._cycle = itertools.cycle(self.proxies)

    @beartype
    def next(self) -> ProxyConfig | None:
        """Get the next available proxy, skipping failed ones."""
        if not self.proxies or self._cycle is None:
            return None

        # Try up to len(proxies) times to find a non-failed one
        for _ in range(len(self.proxies)):
            proxy = next(self._cycle)
            if proxy.server not in self._failed:
                return proxy

        # All failed — reset and try first
        self._failed.clear()
        log.warning("proxy.all_failed_reset")
        return next(self._cycle)

    @beartype
    def mark_failed(self, proxy: ProxyConfig) -> None:
        """Mark a proxy as failed."""
        self._failed.add(proxy.server)
        log.warning("proxy.marked_failed", server=proxy.server)

    @beartype
    def mark_success(self, proxy: ProxyConfig) -> None:
        """Clear failure status for a proxy."""
        self._failed.discard(proxy.server)

    @property
    def available_count(self) -> int:
        """Number of non-failed proxies."""
        return len(self.proxies) - len(self._failed)


@beartype
@require(
    lambda proxy_list: isinstance(proxy_list, str),
    "proxy_list must be a string",
)
def parse_proxy_list(
    proxy_list: str, default_scheme: str = "http"
) -> list[ProxyConfig]:
    """Parse comma-separated proxy list or file path into ProxyConfig list.

    Entries may be:
      - `scheme://host:port` (kept as-is)
      - `host:port` (default_scheme prepended)
      - comment lines starting with `#` (skipped)
      - blank lines (skipped)

    Returns deduplicated list.
    """
    if not proxy_list.strip():
        return []

    path = Path(proxy_list.strip())
    if path.exists() and path.is_file():
        lines = path.read_text().strip().splitlines()
    else:
        lines = [s.strip() for s in proxy_list.split(",")]

    return _normalize_lines(lines, default_scheme)


def _normalize_lines(
    lines: list[str], default_scheme: str
) -> list[ProxyConfig]:
    """Normalize raw lines into deduplicated ProxyConfig list."""
    out: list[ProxyConfig] = []
    seen: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        server = _normalize_entry(line, default_scheme)
        if server is None or server in seen:
            continue
        seen.add(server)
        out.append(ProxyConfig(server=server))
    return out


def _normalize_entry(
    entry: str, default_scheme: str
) -> str | None:
    """Normalize a single proxy entry to scheme://host:port.

    Accepts bare "host:port" (GitHub-raw list convention) as well as
    pre-prefixed forms. Returns None for malformed entries.
    """
    if "://" in entry:
        scheme, _, rest = entry.partition("://")
        scheme = scheme.lower()
        if scheme not in _VALID_SCHEMES:
            return None
        host_port = rest
    else:
        scheme = default_scheme
        host_port = entry
    # Must contain host:port.
    if ":" not in host_port or "/" in host_port:
        return None
    host, _, rest = host_port.partition(":")
    # Some sources (zloi-user/hideip.me) append trailing fields like
    # `host:port:country`. Trim to just the port.
    port = rest.partition(":")[0]
    if not host or not port.isdigit():
        return None
    return f"{scheme}://{host}:{port}"


@beartype
async def fetch_proxy_list_url(
    url: str, timeout: float = 20.0, default_scheme: str = "http"
) -> list[ProxyConfig]:
    """Fetch a newline-separated proxy list from a URL.

    Used with curated GitHub-raw lists (TheSpeedX/PROXY-List,
    monosans/proxy-list, clarketm/proxy-list) which emit
    `host:port` one per line. Returns [] on any error.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code != 200:  # noqa: PLR2004
                log.warning(
                    "proxy.list_url_bad_status",
                    url=url,
                    status=resp.status_code,
                )
                return []
            lines = resp.text.strip().splitlines()
    except Exception as exc:
        log.warning(
            "proxy.list_url_fetch_failed", url=url, error=str(exc)
        )
        return []

    # Lists often embed the scheme in the URL path (http.txt, socks5.txt).
    # Prefer that scheme over the configured default.
    scheme = _infer_scheme_from_url(url) or default_scheme
    parsed = _normalize_lines(lines, scheme)
    log.info("proxy.list_url_loaded", url=url, count=len(parsed))
    return parsed


def _infer_scheme_from_url(url: str) -> str | None:
    """Return a scheme hinted by the URL path, or None.

    Matches the scheme as either a filename (http.txt, socks5.txt) or a
    path segment (/socks5/data.txt, /socks5_list/proxy.txt). Repo/dir
    names containing the scheme token count — most curated SOCKS5-only
    repos encode the protocol in the path.
    """
    lower = url.lower()
    for scheme in ("socks5", "socks4", "https", "http"):
        tokens = (f"/{scheme}.", f"/{scheme}/", f"/{scheme}_")
        if any(t in lower for t in tokens) or lower.endswith(
            f"/{scheme}.txt"
        ):
            return scheme
    return None


@beartype
async def load_proxies(
    proxy_list: str,
    proxy_list_urls: str,
    default_scheme: str = "http",
    max_count: int = 0,
) -> list[ProxyConfig]:
    """Union proxy entries from inline config, file path, and remote URLs.

    Fetches all URLs concurrently. Deduplicates. Truncates to `max_count`
    when > 0; `max_count=0` means no cap (return all).
    """
    proxies: list[ProxyConfig] = list(
        parse_proxy_list(proxy_list, default_scheme)
    )

    urls = [u.strip() for u in proxy_list_urls.split(",") if u.strip()]
    if urls:
        results = await asyncio.gather(
            *(
                fetch_proxy_list_url(
                    u, default_scheme=default_scheme
                )
                for u in urls
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                continue
            proxies.extend(result)

    # Dedupe preserving order.
    seen: set[str] = set()
    unique: list[ProxyConfig] = []
    for p in proxies:
        if p.server in seen:
            continue
        seen.add(p.server)
        unique.append(p)
    # Apply cap only when > 0.
    if max_count > 0 and len(unique) > max_count:
        log.info(
            "proxy.list_truncated",
            total=len(unique),
            kept=max_count,
        )
        return unique[:max_count]
    return unique


@beartype
async def health_check_proxy(
    proxy: ProxyConfig,
    probe_url: str,
    timeout: float = 8.0,
) -> bool:
    """Probe a proxy by fetching `probe_url` through it.

    Returns True iff the GET returns HTTP 200. Categorizes failures
    (configuration, network, HTTP-status) so systemic problems
    (e.g. missing `httpx[socks]`) surface in aggregated stats
    rather than hiding behind a blanket "unhealthy" count.
    """
    # Import here to avoid hard dependency when the module is imported
    # without an asyncio event loop (test helpers etc.).
    from archiver import user_agents as _ua
    try:
        async with httpx.AsyncClient(
            proxy=proxy.server,
            timeout=timeout,
            verify=False,  # noqa: S501
            headers={"User-Agent": _ua.pick()},
        ) as client:
            resp = await client.get(probe_url)
            if resp.status_code == 200:  # noqa: PLR2004
                log.debug("proxy.health_ok", server=proxy.server)
                return True
            log.debug(
                "proxy.health_bad_status",
                server=proxy.server,
                status=resp.status_code,
            )
            return False
    except ImportError as exc:
        # Missing httpx[socks] extras, or similar config problem — this
        # is NOT a per-proxy failure, it's a systemic one. Log loudly.
        log.error(
            "proxy.health_import_error",
            server=proxy.server,
            error=str(exc),
        )
        return False
    except Exception as exc:
        log.debug(
            "proxy.health_fail",
            server=proxy.server,
            error_type=type(exc).__name__,
        )
        return False


@beartype
async def filter_healthy(
    proxies: list[ProxyConfig],
    probe_url: str,
    timeout: float = 8.0,
    concurrency: int = 20,
) -> list[ProxyConfig]:
    """Return the subset of `proxies` that pass health_check_proxy.

    Runs with bounded concurrency so we don't open 10k sockets at once.
    """
    if not proxies:
        return []

    sem = asyncio.Semaphore(concurrency)

    async def _check(
        p: ProxyConfig,
    ) -> tuple[ProxyConfig, bool]:
        async with sem:
            ok = await health_check_proxy(p, probe_url, timeout)
        return p, ok

    results = await asyncio.gather(*(_check(p) for p in proxies))
    healthy = [p for p, ok in results if ok]
    log.info(
        "proxy.health_check_complete",
        total=len(proxies),
        healthy=len(healthy),
    )
    return healthy


# --- ASN-based filtering ------------------------------------------------
# Empirical finding (see tests/integration/test_proxy_gate.py): CF+reCAPTCHA
# on archive.today scores IP reputation heavily by ASN. Proxies from major
# VPS/cloud ASNs get challenged ~100% of the time even through Camoufox+
# SOCKS5; proxies from consumer/regional ISPs pass ~25% of the time.
# Filter out known-datacenter ASNs before attempting gated captures.
_DATACENTER_ASN_KEYWORDS: frozenset[str] = frozenset({
    "hetzner", "ovh", "digitalocean", "linode", "vultr", "contabo",
    "amazon", "aws", "google llc", "google cloud", "googleusercontent",
    "microsoft", "azure", "oracle", "scaleway", "selectel", "cogent",
    "cloudflare", "fastly", "akamai", "colocrossing", "choopa",
    "leaseweb", "datacamp", "frantech", "m247", "constant company",
    "psychz", "nforce", "alibaba", "tencent", "worldstream", "hostkey",
    "quadranet", "serverstadium", "mevspace", "dedipath", "servermania",
    "iomart", "rackspace", "wedos", "eonix", "performive",
})

_asn_lookup_cache: dict[str, dict[str, str]] = {}
_ASN_LOOKUP_URL = "https://ipwho.is/{ip}"


@beartype
async def lookup_asn(
    ip: str, client: httpx.AsyncClient | None = None
) -> dict[str, str]:
    """Return {'org': str, 'country': str} for an IP, or empty on failure.

    Uses ipwho.is (free, no API key, 10k/day). In-memory cached per process.
    """
    if ip in _asn_lookup_cache:
        return _asn_lookup_cache[ip]
    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        resp = await client.get(_ASN_LOOKUP_URL.format(ip=ip))
        if resp.status_code != 200:  # noqa: PLR2004
            return {}
        data = resp.json()
        if not data.get("success"):
            return {}
        info = {
            "org": (data.get("connection", {}) or {}).get("org", "") or "",
            "isp": (data.get("connection", {}) or {}).get("isp", "") or "",
            "country": data.get("country_code", "") or "",
        }
        _asn_lookup_cache[ip] = info
        return info
    except Exception as exc:
        log.debug("proxy.asn_lookup_failed", ip=ip, error=str(exc)[:80])
        return {}
    finally:
        if owned:
            await client.aclose()


@beartype
def is_datacenter_org(org: str) -> bool:
    """Heuristic: does this org/isp string name a major datacenter ASN?

    Match is substring-against-lowercase on the ASN org OR isp fields.
    Conservative — false negatives (missed datacenters) cost little, but
    false positives (flagging consumer ISPs) lose us good proxies.
    """
    if not org:
        return False
    lower = org.lower()
    return any(kw in lower for kw in _DATACENTER_ASN_KEYWORDS)


@beartype
async def filter_by_asn(
    proxies: list[ProxyConfig],
    concurrency: int = 10,
) -> list[ProxyConfig]:
    """Drop proxies whose ASN is a known datacenter/cloud provider.

    The empirical pass rate for datacenter-ASN proxies against CF gates
    is ~0%; removing them up-front saves the slower gate-probe work
    later.
    """
    if not proxies:
        return []
    sem = asyncio.Semaphore(concurrency)

    async def _classify(p: ProxyConfig) -> tuple[ProxyConfig, bool]:
        host = p.server.split("://", 1)[1].split(":", 1)[0]
        async with sem:
            info = await lookup_asn(host)
        dc = is_datacenter_org(info.get("org", "")) or is_datacenter_org(
            info.get("isp", "")
        )
        return p, dc

    results = await asyncio.gather(*(_classify(p) for p in proxies))
    kept = [p for p, dc in results if not dc]
    dropped = len(proxies) - len(kept)
    log.info(
        "proxy.asn_filter_complete",
        total=len(proxies), kept=len(kept), dropped_datacenter=dropped,
    )
    return kept


@beartype
def filter_socks5(proxies: list[ProxyConfig]) -> list[ProxyConfig]:
    """Return only SOCKS5 proxies.

    Empirical: 0/7 HTTP proxies passed the archive.today gate in our
    test run. SOCKS5 was the only protocol with any passes. Use this
    filter before attempting gated-tier captures.
    """
    return [p for p in proxies if p.server.startswith("socks5://")]


# --- Archive.today gate probe -------------------------------------------
# Generic httpbin health check doesn't predict whether a proxy will pass
# the CF+reCAPTCHA gate on archive.today. We need a direct probe.

_GATE_PROBE_URL = "https://archive.ph/"
_GATE_CHALLENGE_MARKERS: tuple[str, ...] = (
    "g-recaptcha", "chk_captcha", "grecaptcha.render",
)


@beartype
async def probe_archive_gate(
    proxy: ProxyConfig,
    timeout: float = 45.0,
) -> bool:
    """Probe archive.ph through `proxy` via Camoufox. True if gate passed.

    "Passed" = HTTP 200 response body lacks the reCAPTCHA markers. A
    proxy that passes here has a clean enough IP reputation for the
    archive.today tier.

    Uses Camoufox (not httpx) because httpx gets fingerprinted
    deterministically by CF — only realistic-browser traffic scores
    high enough to clear the gate.
    """
    from camoufox.async_api import AsyncCamoufox  # type: ignore[import-untyped]

    try:
        async with AsyncCamoufox(
            headless="virtual",
            humanize=True,
            geoip=True,
            proxy={"server": proxy.server},
        ) as browser:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900}
            )
            page = await context.new_page()
            try:
                await page.goto(
                    _GATE_PROBE_URL,
                    wait_until="domcontentloaded",
                    timeout=int(timeout * 1000),
                )
                # Give CF's managed-challenge a few seconds to auto-resolve
                for _ in range(3):
                    await asyncio.sleep(2)
                    html = await page.content()
                    if not any(m in html for m in _GATE_CHALLENGE_MARKERS):
                        log.debug(
                            "proxy.gate_passed", server=proxy.server,
                        )
                        return True
                log.debug("proxy.gate_challenged", server=proxy.server)
                return False
            finally:
                await page.close()
                await context.close()
    except Exception as exc:
        log.debug(
            "proxy.gate_probe_error",
            server=proxy.server,
            error_type=type(exc).__name__,
            error=str(exc)[:120],
        )
        return False


@beartype
async def filter_gate_passing(
    proxies: list[ProxyConfig],
    concurrency: int = 3,
) -> list[ProxyConfig]:
    """Return only proxies that pass the archive.today gate.

    Slow (each probe spawns Camoufox + waits ~6s). Low concurrency to
    avoid opening too many browser instances at once. Run rarely;
    cache results.
    """
    if not proxies:
        return []
    sem = asyncio.Semaphore(concurrency)

    async def _probe(p: ProxyConfig) -> tuple[ProxyConfig, bool]:
        async with sem:
            ok = await probe_archive_gate(p)
        return p, ok

    results = await asyncio.gather(*(_probe(p) for p in proxies))
    passing = [p for p, ok in results if ok]
    log.info(
        "proxy.gate_filter_complete",
        total=len(proxies), passing=len(passing),
    )
    return passing
