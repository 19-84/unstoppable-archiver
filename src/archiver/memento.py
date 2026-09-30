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
miss simply escalates to the next tier. Beyond the timemaps, at most
a handful of newest-first candidate mementos are probed (crawls of
bot-walled or lapsed domains replay the junk that was crawled —
CloudFront challenges, parked-domain pages — so the newest memento is
not always usable), and exactly one gets a browser render.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from beartype import beartype

from archiver.errors import FetchError
from archiver.fallback import mementos_from_timemap
from archiver.http_client import fetch

log = structlog.get_logger()

_TIMEMAP_TIMEOUT_S = 15.0
_TIMEMAP_MAX_BYTES = 4 * 1024 * 1024
_MEMENTO_FETCH_TIMEOUT_S = 20.0
# How many archives to query at once. The roster spans ~11 independent
# organizations, so this is one in-flight request per org at most —
# the bound exists to keep our own socket/latency profile tame.
_PARALLEL = 4
# Candidate budget: newest-first fallback depth when probing mementos.
# Per-archive keeps one archive's dense crawl history from crowding
# the global list; the global cap bounds worst-case probe traffic.
_PER_ARCHIVE_CANDIDATES = 3
_MAX_CANDIDATES = 4


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
async def find_memento_candidates(
    url: str, limit: int = _MAX_CANDIDATES
) -> list[MementoHit]:
    """Query every archive's timemap; return newest candidates overall.

    All archives are consulted (bounded concurrency) rather than
    first-hit-wins: a fast archive with a 2009 copy shouldn't shadow a
    slower one holding last year's. Undated mementos lose to any dated
    one.

    Returns up to `limit` hits, newest first, rather than the single
    winner: the newest memento is not always usable — a crawl of a
    bot-walled site faithfully replays the block itself (empty
    CloudFront 202 challenges) and a lapsed domain replays the parking
    page — so callers probe candidates in order. Empty list when no
    archive has the URL.
    """
    sem = asyncio.Semaphore(_PARALLEL)

    async def _query(archive: MementoArchive) -> list[MementoHit]:
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
                return []
            if resp.status_code != 200:  # noqa: PLR2004
                return []
            return [
                MementoHit(
                    archive_id=archive.id,
                    memento_url=memento_url,
                    timestamp=memento_dt,
                )
                for memento_url, memento_dt in mementos_from_timemap(
                    resp.text
                )[:_PER_ARCHIVE_CANDIDATES]
            ]

    results = await asyncio.gather(
        *(_query(a) for a in MEMENTO_ARCHIVES)
    )
    hits = [h for per_archive in results for h in per_archive]
    if not hits:
        return []

    epoch = datetime.min.replace(tzinfo=UTC)
    hits.sort(key=lambda h: h.timestamp or epoch, reverse=True)
    top = hits[:limit]
    log.info(
        "memento.candidates_found",
        url=url,
        newest=top[0].memento_url,
        archive=top[0].archive_id,
        timestamp=str(top[0].timestamp),
        candidates=len(top),
        archives_with_copies=sum(1 for r in results if r),
    )
    return top


# Replay chrome injected by the wayback-family software the roster
# archives run (OpenWayback and pywb). Stripped before SingleFile when
# a memento is browser-rendered, so the stored snapshot is the original
# page rather than the archive's UI. Selectors that don't match a given
# archive simply remove nothing.
MEMENTO_STRIP_SELECTORS: list[str] = [
    "#wm-ipp",                 # OpenWayback / IA-style toolbar
    "#wm-ipp-base",
    "#_wb_frame_top_banner",   # pywb framed-replay banner
    "#_wb_plain_banner",       # pywb non-framed banner
    'script[src*="default_banner.js"]',
    'script[src*="wombat.js"]',  # pywb client-side rewriter, dead weight
]

# pywb/OpenWayback replay flag: `<ts>if_` serves the bare archived page
# without the archive's replay banner/toolbar chrome. Timemaps may hand
# out memento URLs that already carry a two-letter replay modifier
# (arquivo.pt embeds `mp_`) — match and replace it too, or the raw
# variant is never tried exactly where it matters most.
_REPLAY_TS_RE = re.compile(r"/(\d{14})(?:[a-z]{2}_)?/")


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
) -> tuple[str, str] | None:
    """Fetch a memento's HTML, preferring the raw `if_` replay variant.

    Returns ``(resolved_url, html)`` where ``resolved_url`` is the
    variant that actually served content. Callers that follow up with
    a browser render should render ``resolved_url``: when the archive
    supports ``if_`` it is the banner-free (and, on framed pywb,
    frame-free) form of the page, verified live by this probe.
    """
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
            return (candidate, resp.text)
    return None
