# ABOUTME: Federated Memento (RFC 7089) lookup across national web archives
# ABOUTME: Queries each archive's timemap, picks the newest memento, fetches HTML
"""Federated Memento tier.

Wayback and archive.today are the giants, but a dozen national and
institutional web archives speak the same standardized Memento protocol
(RFC 7089) — one timemap query per archive covers arquivo.pt, the UK
Web Archive, Archive-It's thousands of curated collections, and more.
This module is the single integration for all of them: the archive
roster below is data, not code, so adding a source is one tuple entry.

The roster is curated from the MemGator federation list
(github.com/oduwsdl/MemGator), minus archives we already cover as
dedicated tiers (Internet Archive → wayback, archive.today) and minus
entries marked defunct upstream. Every endpoint below was probed live
on 2026-07-01. Dropped after failing that probe (candidates to re-add
if they recover):

- UK Web Archive (webarchive.org.uk) — connection timeouts even for
  in-scope URLs; access restricted since the British Library incident.
- National Records of Scotland — /timemap/ returns an HTML 404.
- UK Parliament Web Archive — /timemap/ returns 405.
- perma.cc — timemap endpoint sits behind a Cloudflare challenge.

Politeness: one timemap request per archive per job, bounded
concurrency, no retries — these are volunteer/national services and a
miss simply escalates to the next tier.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from beartype import beartype

from archiver.errors import FetchError
from archiver.fallback import latest_memento_from_timemap
from archiver.http_client import fetch

log = structlog.get_logger()

_TIMEMAP_TIMEOUT_S = 15.0
_TIMEMAP_MAX_BYTES = 4 * 1024 * 1024
_MEMENTO_FETCH_TIMEOUT_S = 20.0
# How many archives to query at once. The roster spans ~11 independent
# organizations, so this is one in-flight request per org at most —
# the bound exists to keep our own socket/latency profile tame.
_PARALLEL = 4


@dataclass(frozen=True)
class MementoArchive:
    """One Memento-compliant upstream archive."""

    id: str              # short slug, used in logs and provenance
    name: str            # human-readable, for docs/UI
    timemap_prefix: str  # timemap URL = prefix + original URL


# rel="memento" timemap endpoints. Prefixes end at the point where the
# original URL is appended verbatim.
MEMENTO_ARCHIVES: tuple[MementoArchive, ...] = (
    MementoArchive(
        id="arquivo.pt",
        name="Portuguese Web Archive",
        timemap_prefix="https://arquivo.pt/wayback/timemap/link/",
    ),
    MementoArchive(
        id="archive-it",
        name="Archive-It",
        timemap_prefix="https://wayback.archive-it.org/all/timemap/link/",
    ),
    MementoArchive(
        id="awa",
        name="Australian Web Archive",
        timemap_prefix="https://web.archive.org.au/awa/timemap/link/",
    ),
    MementoArchive(
        id="lac",
        name="Library and Archives Canada",
        timemap_prefix=(
            "https://webarchiveweb.wayback.bac-lac.canada.ca"
            "/web/timemap/link/"
        ),
    ),
    MementoArchive(
        id="banq",
        name="BAnQ (Québec)",
        timemap_prefix="https://waext.banq.qc.ca/wayback/timemap/link/",
    ),
    MementoArchive(
        id="ndl-japan",
        name="National Diet Library, Japan",
        timemap_prefix="https://warp.da.ndl.go.jp/collections/timemap/",
    ),
    MementoArchive(
        id="vefsafn",
        name="Icelandic Web Archive",
        timemap_prefix="https://vefsafn.is/timemap/link/",
    ),
)


@dataclass(frozen=True)
class MementoHit:
    """Newest memento one archive holds for a URL."""

    archive_id: str
    memento_url: str
    timestamp: datetime | None  # from the timemap's datetime attribute


@beartype
async def find_latest_memento(url: str) -> MementoHit | None:
    """Query every archive's timemap; return the newest memento overall.

    All archives are consulted (bounded concurrency) rather than
    first-hit-wins: a fast archive with a 2009 copy shouldn't shadow a
    slower one holding last year's. Undated mementos lose to any dated
    one. None when no archive has the URL.
    """
    sem = asyncio.Semaphore(_PARALLEL)

    async def _query(archive: MementoArchive) -> MementoHit | None:
        async with sem:
            try:
                resp = await fetch(
                    archive.timemap_prefix + url,
                    timeout=_TIMEMAP_TIMEOUT_S,
                    follow_redirects=True,
                    attempts=1,
                    max_bytes=_TIMEMAP_MAX_BYTES,
                    guard_private_ips=True,
                )
            except FetchError as exc:
                log.debug(
                    "memento.timemap_error",
                    archive=archive.id,
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
                return None
            if resp.status_code != 200:  # noqa: PLR2004
                return None
            parsed = latest_memento_from_timemap(resp.text)
            if parsed is None:
                return None
            memento_url, memento_dt = parsed
            # Timemap datetimes are RFC 1123 GMT per spec, but a
            # missing zone would arrive naive and poison the
            # TIMESTAMPTZ write later — pin to UTC.
            if memento_dt is not None and memento_dt.tzinfo is None:
                memento_dt = memento_dt.replace(tzinfo=UTC)
            return MementoHit(
                archive_id=archive.id,
                memento_url=memento_url,
                timestamp=memento_dt,
            )

    results = await asyncio.gather(
        *(_query(a) for a in MEMENTO_ARCHIVES)
    )
    hits = [h for h in results if h is not None]
    if not hits:
        return None

    epoch = datetime.min.replace(tzinfo=UTC)
    best = max(hits, key=lambda h: h.timestamp or epoch)
    log.info(
        "memento.snapshot_found",
        url=url,
        archive=best.archive_id,
        memento=best.memento_url,
        timestamp=str(best.timestamp),
        archives_with_copies=len(hits),
    )
    return best


# pywb/OpenWayback replay flag: `<ts>if_` serves the bare archived page
# without the archive's replay banner/toolbar chrome.
_REPLAY_TS_RE = re.compile(r"/(\d{14})/")


def _raw_replay_variants(memento_url: str) -> list[str]:
    """Return [raw `if_` variant, original], or just [original].

    The raw variant keeps banner markup out of our stored HTML and the
    search index. Archives that don't support the flag 404 it and we
    fall back to the plain memento URL.
    """
    raw = _REPLAY_TS_RE.sub(r"/\1if_/", memento_url, count=1)
    if raw == memento_url:
        return [memento_url]
    return [raw, memento_url]


@beartype
async def fetch_memento_html(
    memento_url: str, timeout: float = _MEMENTO_FETCH_TIMEOUT_S
) -> str | None:
    """Fetch a memento's HTML, preferring the raw `if_` replay variant."""
    for candidate in _raw_replay_variants(memento_url):
        try:
            resp = await fetch(
                candidate,
                timeout=timeout,
                follow_redirects=True,
                attempts=1,
                guard_private_ips=True,
            )
        except FetchError as exc:
            log.debug(
                "memento.fetch_error",
                url=candidate,
                error_type=type(exc).__name__,
                error=str(exc)[:120],
            )
            continue
        if resp.status_code == 200 and resp.content:  # noqa: PLR2004
            return resp.text
    return None
