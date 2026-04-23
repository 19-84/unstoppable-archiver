#!/usr/bin/env python3
# ABOUTME: Curated-URL availability probe across Wayback/archive.today/CC
# ABOUTME: One-shot data collection for tier-routing analysis — respects upstream
"""Probe a curated URL set for availability in each upstream archive.

For each URL, asks:
  * Does Wayback have a snapshot? (`archive.org/wayback/available`)
  * Does archive.today have a timemap entry? (across all 7 mirrors)
  * Does Common Crawl have a record in the 3 newest crawls?

Emits a markdown table + per-category summary. No captures are run.
All queries are serial and paced — no bulk parallel fan-out against
upstream free services (respects our rate-limit policy).

Run:
    uv run python scripts/analyze_availability.py

Output goes to stdout and a timestamped JSON file under /tmp.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archiver.commoncrawl import find_snapshot as cc_find_snapshot
from archiver.config import Settings
from archiver.db import close_pool, create_pool
from archiver.fallback import (
    check_wayback_availability,
    find_archive_today_snapshot,
)
from archiver.repository import ProxyStatusRepository

# Categories reflect hostility axes we actually care about for routing,
# not editorial topic. The axis is: how do anti-bot/anti-archive
# mechanisms differ across these classes?
CURATED: dict[str, list[str]] = {
    "news_permissive": [
        "https://www.bbc.com/news",
        "https://www.theguardian.com/international",
        "https://www.npr.org/",
        "https://www.reuters.com/",
        "https://apnews.com/",
        "https://www.aljazeera.com/",
    ],
    "news_paywalled": [
        "https://www.wsj.com/",
        "https://www.nytimes.com/",
        "https://www.ft.com/",
        "https://www.bloomberg.com/",
        "https://www.economist.com/",
        "https://www.washingtonpost.com/",
    ],
    "social_ugc": [
        "https://old.reddit.com/r/technology",
        "https://news.ycombinator.com/",
        "https://medium.com/",
        "https://substack.com/",
        "https://www.tumblr.com/",
        "https://mastodon.social/",
    ],
    "tech_docs": [
        "https://github.com/torvalds/linux",
        "https://developer.mozilla.org/en-US/",
        "https://stackoverflow.com/",
        "https://en.wikipedia.org/wiki/Web_archiving",
        "https://docs.python.org/3/",
    ],
    "government": [
        "https://www.whitehouse.gov/",
        "https://www.gov.uk/",
        "https://www.congress.gov/",
        "https://www.europarl.europa.eu/",
        "https://www.un.org/",
    ],
    "cf_protected": [
        "https://archive.today/",
        "https://nitter.net/",
        "https://www.cloudflare.com/",
        "https://discord.com/",
        "https://www.patreon.com/",
    ],
}

# Spacing between probes against the same upstream. Conservative enough
# to keep us well under any rate limit — one-shot run, not a crawler.
_SPACING_S = 0.8


@dataclass
class Result:
    url: str
    category: str
    wayback: bool
    archive_today: bool
    commoncrawl: bool
    elapsed_s: float
    error: str | None


async def _probe_one(
    url: str, category: str, at_proxy: str | None
) -> Result:
    t0 = time.perf_counter()
    error: str | None = None

    # Run each probe sequentially so one slow upstream doesn't push us
    # above rate limits on the others.
    try:
        wb = await check_wayback_availability(url)
    except Exception as exc:
        wb = None
        error = f"wayback: {exc!s}"
    await asyncio.sleep(_SPACING_S)

    # archive.today probes route through a gate-passing SOCKS5 proxy
    # when one is available — the CF edge scores our direct IP poorly,
    # so an unproxied timemap query is ~always walled.
    try:
        at = await find_archive_today_snapshot(url, proxy=at_proxy)
    except Exception as exc:
        at = None
        error = (error + " | " if error else "") + f"archive.today: {exc!s}"
    await asyncio.sleep(_SPACING_S)

    try:
        cc = await cc_find_snapshot(url)
    except Exception as exc:
        cc = None
        error = (error + " | " if error else "") + f"commoncrawl: {exc!s}"

    return Result(
        url=url,
        category=category,
        wayback=wb is not None,
        archive_today=at is not None,
        commoncrawl=cc is not None,
        elapsed_s=round(time.perf_counter() - t0, 2),
        error=error,
    )


def _summarize(results: list[Result]) -> dict[str, dict[str, float]]:
    """Aggregate per-category hit rates per upstream."""
    by_cat: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        by_cat[r.category].append(r)

    summary: dict[str, dict[str, float]] = {}
    for cat, items in by_cat.items():
        n = len(items)
        summary[cat] = {
            "n": n,
            "wayback_pct": round(sum(r.wayback for r in items) / n * 100, 1),
            "archive_today_pct": round(
                sum(r.archive_today for r in items) / n * 100, 1
            ),
            "commoncrawl_pct": round(
                sum(r.commoncrawl for r in items) / n * 100, 1
            ),
            "any_upstream_pct": round(
                sum(
                    r.wayback or r.archive_today or r.commoncrawl for r in items
                ) / n * 100,
                1,
            ),
        }
    return summary


def _print_table(results: list[Result]) -> None:
    print()
    print(
        f"{'URL':<55} {'cat':<18} {'WB':>3} {'AT':>3} {'CC':>3} {'s':>5}"
    )
    print("-" * 94)
    for r in results:
        wb = "y" if r.wayback else "."
        at = "y" if r.archive_today else "."
        cc = "y" if r.commoncrawl else "."
        url = r.url if len(r.url) <= 54 else r.url[:51] + "..."  # noqa: PLR2004
        print(
            f"{url:<55} {r.category:<18} "
            f"{wb:>3} {at:>3} {cc:>3} {r.elapsed_s:>5.1f}"
        )


def _print_summary(summary: dict[str, dict[str, float]]) -> None:
    print()
    print(
        f"{'category':<20} {'n':>3} "
        f"{'WB%':>6} {'AT%':>6} {'CC%':>6} {'any%':>6}"
    )
    print("-" * 55)
    for cat, s in summary.items():
        print(
            f"{cat:<20} {int(s['n']):>3} "
            f"{s['wayback_pct']:>6.1f} "
            f"{s['archive_today_pct']:>6.1f} "
            f"{s['commoncrawl_pct']:>6.1f} "
            f"{s['any_upstream_pct']:>6.1f}"
        )


async def _pick_gate_proxy() -> str | None:
    """Read a fresh gate-passing SOCKS5 from proxy_status, or None."""
    settings = Settings()
    pool = await create_pool(
        settings.db_url.get_secret_value(), min_size=1, max_size=2
    )
    try:
        repo = ProxyStatusRepository()
        async with pool.acquire() as conn:
            passing = await repo.list_passing(conn, max_age_hours=24)
    finally:
        await close_pool(pool)
    if not passing:
        return None
    import random
    return random.choice(passing)  # noqa: S311 — not security-sensitive


async def main() -> int:
    urls = [(u, c) for c, lst in CURATED.items() for u in lst]

    at_proxy = await _pick_gate_proxy()
    if at_proxy is None:
        print(
            "WARNING: no gate-passing proxies in DB — archive.today "
            "probes will go direct (expect near-zero hits)",
            flush=True,
        )
    else:
        # Log scheme/host only, not full server string — the script
        # output may be shared and proxy URLs are sensitive.
        host = at_proxy.split("://", 1)[1].split(":", 1)[0]
        print(
            f"Using gate-passing SOCKS5 proxy {host}:*** for archive.today",
            flush=True,
        )

    print(
        f"Probing {len(urls)} URLs across {len(CURATED)} categories "
        f"(serial, ~{_SPACING_S * 3 * len(urls):.0f}s min)...",
        flush=True,
    )
    print()

    results: list[Result] = []
    for idx, (url, cat) in enumerate(urls, 1):
        print(f"[{idx}/{len(urls)}] {url}", flush=True)
        r = await _probe_one(url, cat, at_proxy)
        results.append(r)

    _print_table(results)
    summary = _summarize(results)
    _print_summary(summary)

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out = Path(f"/tmp/availability_{stamp}.json")  # noqa: S108
    out.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "results": [asdict(r) for r in results],
                "summary": summary,
            },
            indent=2,
        )
    )
    print(f"\nRaw: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
