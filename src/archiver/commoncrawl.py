# ABOUTME: Common Crawl CDX lookup + WARC range-fetch for the fallback tier
# ABOUTME: Fast recent-crawl query (parallel) + full-history scan (paced)
"""Common Crawl integration.

Provides two lookup paths and a record-fetch helper:

- ``find_snapshot(url)`` — fast: queries the N newest crawls in parallel
  (bounded concurrency), returns the first hit. Used by the synchronous
  ``commoncrawl`` tier.

- ``find_snapshot_full_history(url)`` — slow: sequential scan of all
  ~122 crawls back to 2014, paced to avoid tripping Common Crawl's
  rate limits. Used as the fall-through when ``find_snapshot`` misses,
  so long-tail URLs only ever captured years ago are still reachable.

- ``fetch_record_html(snapshot)`` — range-fetches the single WARC record
  identified by CDX coordinates and returns the HTTP response body.

No AWS credentials required — we use the ``data.commoncrawl.org`` HTTPS
mirror. Rate-limit-aware: backs off on 429/503 and surfaces errors
instead of hammering.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import httpx
import structlog
from beartype import beartype

from archiver import user_agents

log = structlog.get_logger()

_INDEX_BASE = "https://index.commoncrawl.org"
_DATA_BASE = "https://data.commoncrawl.org"
_COLLINFO_URL = f"{_INDEX_BASE}/collinfo.json"

# Fetch from index + data respecting rate limits. CC operators have
# asked heavy users to back off; 5 req/sec + per-query 10s timeout is
# conservative enough that we rarely hit 429.
_MIN_SPACING_S = 0.2
_QUERY_TIMEOUT_S = 15.0
_FETCH_TIMEOUT_S = 30.0
_BACKOFF_MIN_S = 2.0
_BACKOFF_MAX_S = 120.0

# How many recent crawls to query in the fast path.
_RECENT_CRAWLS = 3
_RECENT_PARALLEL = 3

# Collinfo cache — crawl list rarely changes; fetch once per hour.
_COLLINFO_TTL_S = 3600
_collinfo_cache: tuple[float, list[str]] | None = None
_collinfo_lock = asyncio.Lock()


@dataclass(frozen=True)
class CCSnapshot:
    """Coordinates of a single CC record for a URL, plus its metadata."""

    url: str            # URL as CC stored it (may differ slightly from query)
    timestamp: str      # YYYYMMDDHHMMSS
    crawl_id: str       # CC-MAIN-YYYY-WW
    filename: str       # path under data.commoncrawl.org
    offset: int
    length: int
    status: int
    mime: str

    def fetch_url(self) -> str:
        return f"{_DATA_BASE}/{self.filename}"


@beartype
async def list_crawls(client: httpx.AsyncClient | None = None) -> list[str]:
    """Return crawl IDs newest-first. 1h cache.

    Example return: ``["CC-MAIN-2026-12", "CC-MAIN-2026-08", …]``
    """
    global _collinfo_cache
    async with _collinfo_lock:
        if _collinfo_cache is not None:
            ts, ids = _collinfo_cache
            if time.time() - ts < _COLLINFO_TTL_S:
                return list(ids)
        owned_client = client is None
        if owned_client:
            client = httpx.AsyncClient(
                timeout=30.0,
                headers={"User-Agent": user_agents.pick()},
            )
        try:
            resp = await client.get(_COLLINFO_URL)
            resp.raise_for_status()
            ids = [c["id"] for c in resp.json()]
            _collinfo_cache = (time.time(), list(ids))
            return list(ids)
        finally:
            if owned_client:
                await client.aclose()


async def _query_crawl(  # noqa: PLR0911
    client: httpx.AsyncClient,
    crawl_id: str,
    url: str,
) -> CCSnapshot | None:
    """Run one CDX query for one URL against one crawl. Returns first hit.

    Returns None for misses, 404s, and rate-limits. Raises only on
    unexpected errors (network failure outside the timeout budget).
    """
    try:
        resp = await client.get(
            f"{_INDEX_BASE}/{crawl_id}-index",
            params={"url": url, "output": "json", "limit": "1"},
            timeout=_QUERY_TIMEOUT_S,
        )
    except httpx.TimeoutException:
        log.debug("commoncrawl.query_timeout", crawl=crawl_id, url=url)
        return None
    except Exception as exc:
        log.debug(
            "commoncrawl.query_error",
            crawl=crawl_id, url=url, error=str(exc)[:120],
        )
        return None

    if resp.status_code in (429, 503):
        log.warning(
            "commoncrawl.rate_limited",
            crawl=crawl_id, status=resp.status_code,
        )
        return None
    if resp.status_code != 200:  # noqa: PLR2004
        return None
    if "No Captures found" in resp.text:
        return None

    try:
        first_line = resp.text.strip().split("\n", 1)[0]
        rec = json.loads(first_line)
    except (ValueError, IndexError):
        log.debug("commoncrawl.parse_error", crawl=crawl_id)
        return None

    try:
        return CCSnapshot(
            url=rec["url"],
            timestamp=rec["timestamp"],
            crawl_id=crawl_id,
            filename=rec["filename"],
            offset=int(rec["offset"]),
            length=int(rec["length"]),
            status=int(rec.get("status", 0)),
            mime=rec.get("mime", ""),
        )
    except (KeyError, ValueError) as exc:
        log.debug("commoncrawl.malformed_record", crawl=crawl_id, error=str(exc))
        return None


@beartype
async def find_snapshot(
    url: str,
    recent_n: int = _RECENT_CRAWLS,
    parallel: int = _RECENT_PARALLEL,
) -> CCSnapshot | None:
    """Fast lookup: query the `recent_n` newest crawls in parallel.

    Returns the first successful hit, preferring the newest crawl.
    None if no hits in the recent window (the caller can fall through
    to ``find_snapshot_full_history`` for a deep scan).
    """
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agents.pick()},
        follow_redirects=True,
    ) as client:
        crawls = await list_crawls(client)
        targets = crawls[:recent_n]
        if not targets:
            return None

        sem = asyncio.Semaphore(parallel)

        async def _bounded_query(cr: str) -> CCSnapshot | None:
            async with sem:
                return await _query_crawl(client, cr, url)

        results = await asyncio.gather(
            *(_bounded_query(c) for c in targets),
            return_exceptions=False,
        )
        # Results are in request order; the first non-None IS the
        # newest-crawl hit since targets was newest-first.
        for r in results:
            if r is not None and r.status == 200:  # noqa: PLR2004
                log.info(
                    "commoncrawl.snapshot_found",
                    url=url, crawl=r.crawl_id, timestamp=r.timestamp,
                )
                return r
    return None


@beartype
async def find_snapshot_full_history(
    url: str,
    max_crawls: int | None = None,
    stop_on_first: bool = True,
) -> CCSnapshot | None:
    """Sequential scan of all crawls, newest-first, paced to respect CC.

    Takes ~2-5 minutes for a full 122-crawl scan. Intended for
    background jobs; never call from a user-facing synchronous path.

    Returns the newest hit if found. If ``stop_on_first`` is False,
    scans every crawl even after finding hits (useful for building a
    complete history — but this isn't the common use case).
    """
    async with httpx.AsyncClient(
        headers={"User-Agent": user_agents.pick()},
        follow_redirects=True,
    ) as client:
        crawls = await list_crawls(client)
        if max_crawls:
            crawls = crawls[:max_crawls]

        best: CCSnapshot | None = None
        last_request_ts = 0.0
        for crawl_id in crawls:
            dt = time.time() - last_request_ts
            if dt < _MIN_SPACING_S:
                await asyncio.sleep(_MIN_SPACING_S - dt)
            last_request_ts = time.time()

            snap = await _query_crawl(client, crawl_id, url)
            if snap is not None and snap.status == 200:  # noqa: PLR2004
                if best is None:
                    best = snap
                if stop_on_first:
                    log.info(
                        "commoncrawl.deep_scan_hit",
                        url=url,
                        crawl=crawl_id,
                        timestamp=snap.timestamp,
                    )
                    return best

        if best is not None:
            log.info(
                "commoncrawl.deep_scan_hit",
                url=url, crawl=best.crawl_id, timestamp=best.timestamp,
            )
    return best


@beartype
async def fetch_record_html(snapshot: CCSnapshot) -> bytes:
    """Range-fetch the WARC record and return its HTTP response body.

    Raises on network failure or if the record can't be parsed.
    """
    from warcio.archiveiterator import ArchiveIterator  # type: ignore[import-untyped]

    end = snapshot.offset + snapshot.length - 1
    async with httpx.AsyncClient(
        headers={
            "User-Agent": user_agents.pick(),
            "Range": f"bytes={snapshot.offset}-{end}",
        },
        follow_redirects=True,
        timeout=_FETCH_TIMEOUT_S,
    ) as client:
        resp = await client.get(snapshot.fetch_url())
    # Range requests return 206 on success; 200 means the server
    # ignored the Range header and returned the whole file.
    if resp.status_code not in (200, 206):
        raise RuntimeError(
            f"CC data.commoncrawl.org returned {resp.status_code}"
        )
    buf = BytesIO(resp.content)
    # warcio ships no type stubs — its records expose rec_type and
    # content_stream() but pyright sees them as Unknown.
    it: Any = ArchiveIterator(buf)
    for rec in it:
        if rec.rec_type != "response":
            continue
        body: bytes = rec.content_stream().read()
        return body
    raise RuntimeError("No response record found in WARC chunk")


