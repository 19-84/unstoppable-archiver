#!/usr/bin/env python3
# ABOUTME: One-shot probe of every registered privacy-frontend instance
# ABOUTME: Validates we can actually reach a real post through Camoufox+SOCKS5
"""Probe every privacy-frontend instance and report which serve content.

Runs the same two-stage health check the worker uses:
  1) is_alive_tcp() — drops hosts that don't resolve / refuse :443
  2) probe_frontend_instance() — Camoufox + gate-passing SOCKS5,
     marker check against body (post-<head>-strip)

Results are persisted to the frontend_status table (same as the
hourly worker loop) and printed as a summary table.

Run inside the worker container so Camoufox's binary cache, the
prim ed SOCKS5 pool, and the DB env are all available:

    docker compose exec worker python scripts/probe_nitter_pool.py

Optional --apex flag restricts the probe to one policy
(e.g. --apex twitter.com).
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archiver.config import Settings
from archiver.db import close_pool, create_pool, init_db
from archiver.privacy_frontends import (
    FRONTENDS,
    discover_instances,
    is_alive_tcp,
    probe_frontend_instance,
)
from archiver.repository import FrontendStatusRepository, ProxyStatusRepository


async def _pick_proxy(pool) -> str | None:
    """Random gate-passing SOCKS5 from the primed pool."""
    repo = ProxyStatusRepository()
    async with pool.acquire() as conn:
        passing = await repo.list_passing(conn)
    if not passing:
        return None
    return passing[secrets.randbelow(len(passing))]


async def _probe_one(
    pool,
    policy,
    instance: str,
    proxy: str,
    frontend_repo: FrontendStatusRepository,
) -> dict[str, object]:
    """Run TCP pre-filter then Camoufox content probe. Persist result."""
    host = urlparse(instance).hostname or ""
    t0 = time.monotonic()
    alive = await is_alive_tcp(host) if host else False
    if not alive:
        outcome = {
            "instance": instance,
            "apex": policy.target_apex,
            "stage": "tcp",
            "passing": False,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "note": "TCP unreachable",
        }
    else:
        try:
            passing = await probe_frontend_instance(policy, instance, proxy)
            note = "content marker found" if passing else "no marker in body"
        except Exception as exc:
            passing = False
            note = f"err: {type(exc).__name__}: {str(exc)[:80]}"
        outcome = {
            "instance": instance,
            "apex": policy.target_apex,
            "stage": "camoufox",
            "passing": passing,
            "elapsed_s": round(time.monotonic() - t0, 2),
            "note": note,
        }
    async with pool.acquire() as conn:
        await frontend_repo.record(
            conn,
            instance,
            policy.target_apex,
            content_verified=bool(outcome["passing"]),
        )
    return outcome


async def main(apex_filter: str | None) -> int:
    settings = Settings()
    pool = await create_pool(
        settings.db_url.get_secret_value(), min_size=2, max_size=5,
    )
    await init_db(pool)
    try:
        proxy = await _pick_proxy(pool)
        if proxy is None:
            print(
                "ERROR: no gate-passing SOCKS5 in proxy_status. "
                "Run scripts/prime_gate_passing_pool.py first.",
                file=sys.stderr,
            )
            return 2
        print(f"using proxy: {proxy}")

        frontend_repo = FrontendStatusRepository()
        results: list[dict[str, object]] = []
        for policy in FRONTENDS:
            if apex_filter and policy.target_apex != apex_filter:
                continue
            # Union static fallback with live upstream registry.
            instances = await discover_instances(policy)
            extra = len(instances) - len(policy.instances)
            print(
                f"  policy {policy.target_apex} -> {len(instances)} instances "
                f"({len(policy.instances)} static"
                f"{f', +{extra} from registry' if extra > 0 else ''})",
                flush=True,
            )
            for instance in instances:
                print(
                    f"  probing {policy.target_apex:14s} {instance}",
                    flush=True,
                )
                r = await _probe_one(
                    pool, policy, instance, proxy, frontend_repo,
                )
                tag = "PASS" if r["passing"] else "FAIL"
                print(
                    f"    -> [{tag}] {r['elapsed_s']:.1f}s "
                    f"({r['stage']}) {r['note']}",
                    flush=True,
                )
                results.append(r)

        print("\n=== Summary ===")
        for r in results:
            tag = "PASS" if r["passing"] else "FAIL"
            print(
                f"  [{tag}] {r['apex']:14s} {r['instance']:42s} "
                f"{r['elapsed_s']:5.1f}s {r['note']}"
            )
        passing_count = sum(1 for r in results if r["passing"])
        print(
            f"\n  {passing_count}/{len(results)} instances passed "
            "content-positive probe.",
        )
        return 0 if passing_count > 0 else 1
    finally:
        await close_pool(pool)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--apex",
        help="restrict probe to one target_apex (e.g. twitter.com)",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.apex)))
