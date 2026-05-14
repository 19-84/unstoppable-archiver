#!/usr/bin/env python3
# ABOUTME: Submit a URL as a capture job starting at a specific tier
# ABOUTME: Bypasses the normal tier-1 entry point so we can exercise any tier directly
"""Submit a URL at a specific CaptureTier.

Useful for validating a single tier in isolation — e.g. exercising
PRIVACY_FRONTEND directly instead of waiting ~3 min for tiers 1-3 to
fail on a URL they'd ultimately escalate from.

Usage:
    uv run python scripts/submit_at_tier.py <url> <tier>

Example:
    ... scripts/submit_at_tier.py https://medium.com/@vgr/foo privacy_frontend
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archiver.config import Settings
from archiver.db import close_pool, create_pool, init_db
from archiver.enums import CaptureTier
from archiver.repository import ArchiveRepository, JobRepository


async def main(url: str, tier_str: str) -> int:
    try:
        tier = CaptureTier(tier_str)
    except ValueError:
        valid = ", ".join(t.value for t in CaptureTier)
        print(f"invalid tier {tier_str!r}; expected one of: {valid}", file=sys.stderr)
        return 1

    settings = Settings()
    pool = await create_pool(
        settings.db_url.get_secret_value(), min_size=1, max_size=2
    )
    try:
        await init_db(pool)
        archive_repo = ArchiveRepository()
        job_repo = JobRepository()
        async with pool.acquire() as conn:
            archive = await archive_repo.create(conn, url)
            await job_repo.enqueue(conn, archive.id, tier)
        print(f"enqueued archive={archive.id} url={url} tier={tier.value}")
        return 0
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    if len(sys.argv) != 3:  # noqa: PLR2004
        print(
            "usage: submit_at_tier.py <url> <tier>",
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1], sys.argv[2])))
