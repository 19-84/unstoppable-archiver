# ABOUTME: Rotating pool of current real-world User-Agent strings
# ABOUTME: Refreshed daily from a public top-UAs dataset; bundled fallback if offline
"""User-Agent rotation for blending into real-world browser traffic.

**We never identify ourselves as an archiver** — the project name, version,
or purpose never appears in any outbound header. Capture success rate
depends on looking like an ordinary user's browser.

Daily refresh from Jonathan Rembold's `top-user-agents` dataset, which
is rebuilt every 24h from real web traffic (Chrome/Firefox/Safari/Edge
version share, current builds). Falls back to a baked-in pool if the
source is unreachable.

Usage:

    from archiver.user_agents import pick, refresh

    headers = {"User-Agent": pick()}  # new random UA each call
    # In async contexts, call `await refresh()` periodically (hourly
    # is plenty; daily is the refresh cadence of the upstream source).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

import httpx
import structlog
from beartype import beartype

log = structlog.get_logger()

# Jonathan Rembold's daily-updated top UAs list — plain JSON array of
# strings, rebuilt from real-world traffic stats every 24h. Small (a few
# KB), no auth, no rate limit. https://github.com/jnrbsn/user-agents
_SOURCE_URL = "https://jnrbsn.github.io/user-agents/user-agents.json"

# Cache file on disk so workers restarting within a day skip refetch.
# /tmp is fine — it's disposable data we can regenerate.
_CACHE_PATH = Path("/tmp/archiver_user_agents.json")  # noqa: S108
_CACHE_TTL_SECONDS = 24 * 3600

# Conservative fallback pool refreshed when shipping a new release.
# All strings reflect real-world top-share browsers as of 2026-04.
# When the network refresh works (typical case), this is irrelevant.
_BUNDLED_POOL: tuple[str, ...] = (
    # Chrome on Windows 10/11 — largest real-world share
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0)"
    " Gecko/20100101 Firefox/140.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0)"
    " Gecko/20100101 Firefox/128.0",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0)"
    " Gecko/20100101 Firefox/140.0",
    # Safari on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
    " AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
)

# Module state — mutated by refresh(). Starts with the bundled pool so
# every call to pick() works even before the first refresh.
_pool: list[str] = list(_BUNDLED_POOL)
_last_refresh_ts: float = 0.0
_refresh_lock = asyncio.Lock()


@beartype
def pick() -> str:
    """Return a random UA from the current pool.

    Safe to call from any thread / any moment — the module always has
    at least the bundled pool available.
    """
    return random.choice(_pool)  # noqa: S311


@beartype
def current_pool_size() -> int:
    """How many UAs are currently in rotation — diagnostic only."""
    return len(_pool)


@beartype
async def refresh(force: bool = False) -> int:
    """Refresh the pool from the remote source if stale.

    Returns the new pool size. No-op if the in-memory pool was already
    refreshed within `_CACHE_TTL_SECONDS`. Network failure falls through
    to the on-disk cache (if recent enough) or the bundled pool.

    Pass `force=True` to bypass the staleness check (e.g. on startup).
    """
    global _pool, _last_refresh_ts
    async with _refresh_lock:
        now = time.time()
        if not force and (now - _last_refresh_ts) < _CACHE_TTL_SECONDS:
            return len(_pool)

        # Try on-disk cache next if it's fresh enough.
        if _CACHE_PATH.exists():
            try:
                mtime = _CACHE_PATH.stat().st_mtime
                if (now - mtime) < _CACHE_TTL_SECONDS:
                    data = json.loads(_CACHE_PATH.read_text())
                    if _is_valid_pool(data):
                        _pool = list(data)
                        _last_refresh_ts = mtime
                        log.debug(
                            "user_agents.loaded_from_cache",
                            count=len(_pool),
                        )
                        return len(_pool)
            except Exception as exc:
                log.debug(
                    "user_agents.cache_read_failed", error=str(exc)
                )

        # Fetch fresh.
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                resp = await c.get(_SOURCE_URL)
            if resp.status_code == 200 and _is_valid_pool(resp.json()):  # noqa: PLR2004
                data = resp.json()
                _pool = list(data)
                _last_refresh_ts = now
                try:
                    _CACHE_PATH.write_text(json.dumps(data))
                except Exception as exc:
                    log.debug(
                        "user_agents.cache_write_failed", error=str(exc)
                    )
                log.info("user_agents.refreshed", count=len(_pool))
                return len(_pool)
            log.warning(
                "user_agents.refresh_bad_response",
                status=resp.status_code,
            )
        except Exception as exc:
            log.warning("user_agents.refresh_failed", error=str(exc))

        # Last resort — we already have the bundled pool loaded by
        # module init, so there's always something to pick from.
        if not _pool:
            _pool = list(_BUNDLED_POOL)
        _last_refresh_ts = now
        return len(_pool)


def _is_valid_pool(data: object) -> bool:
    """Sanity check: pool must be a non-empty list of non-trivial strings."""
    if not isinstance(data, list) or not data:
        return False
    return all(isinstance(s, str) and len(s) >= 40 for s in data)  # noqa: PLR2004
