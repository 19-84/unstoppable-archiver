#!/usr/bin/env python3
# ABOUTME: Populate proxy_status with SOCKS5 proxies that clear archive.today's CF gate
# ABOUTME: One-shot priming; worker reads from proxy_status to route tier-5 reads
"""Prime the gate-passing proxy pool.

Loads the same SOCKS5 proxy lists the worker uses, drops datacenter
ASNs up front (empirical ~0% pass rate), then runs the Camoufox-based
archive.ph gate probe on a bounded subset and persists the winners
via ProxyStatusRepository.

Deliberately SKIPS the httpbin.org health check — httpbin rate-limits
us aggressively and the gate probe itself filters unreachable SOCKS
endpoints (Camoufox returns a clean failure if it can't connect).

Run:
    uv run python scripts/prime_gate_passing_pool.py [--max-candidates N]

Each probe takes ~6-15 s (Camoufox cold start + page load + CF wait).
Concurrency is kept low (3) to stay under archive.ph's edge rate limits.

Default cap of 100 candidates gives ~5-10 min of runtime and — at the
prior ~25 % pass rate for consumer-ASN proxies — should yield 20-30
gate-passing entries which is plenty for tier-5 rotation.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archiver.config import Settings
from archiver.db import close_pool, create_pool, init_db
from archiver.proxy import (
    ProxyConfig,
    filter_by_asn,
    filter_socks5,
    load_proxies,
    probe_archive_gate,
)
from archiver.repository import ProxyStatusRepository


async def _tcp_reachable(proxy: ProxyConfig, timeout: float = 3.0) -> bool:
    """Open a TCP connection to the proxy's host:port; close immediately.

    Cheap (< timeout s) dead-endpoint filter. Independent of any HTTP
    service, so unlike httpbin it can't get us rate-limited. Catches
    the majority of dead entries from public lists before we spend 5-
    125 s each on the full Camoufox gate probe.
    """
    try:
        # proxy.server is "socks5://host:port"
        hostport = proxy.server.split("://", 1)[1]
        host, port_s = hostport.rsplit(":", 1)
        port = int(port_s)
    except (IndexError, ValueError):
        return False
    import contextlib
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout,
        )
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True
    except Exception:
        return False


async def _tcp_filter(
    proxies: list[ProxyConfig], concurrency: int = 100
) -> list[ProxyConfig]:
    """Keep only proxies whose host:port accepts a TCP connection."""
    sem = asyncio.Semaphore(concurrency)

    async def _one(p: ProxyConfig) -> tuple[ProxyConfig, bool]:
        async with sem:
            ok = await _tcp_reachable(p)
        return p, ok

    results = await asyncio.gather(*(_one(p) for p in proxies))
    return [p for p, ok in results if ok]


async def _load_and_prefilter(
    settings: Settings, max_candidates: int
) -> list[ProxyConfig]:
    """Load proxy lists + SOCKS5 + TCP liveness + ASN filters + cap."""
    print("Loading proxy lists...", flush=True)
    raw = await load_proxies(
        settings.proxy_list,
        settings.proxy_list_urls,
        default_scheme=settings.proxy_default_scheme,
        max_count=0,
    )
    print(f"  loaded: {len(raw)} total", flush=True)

    socks = filter_socks5(raw)
    print(f"  after socks5-only: {len(socks)}", flush=True)

    # TCP liveness first — the cheapest filter, drops the ~70-90% of
    # public-list entries whose host:port no longer accepts connections.
    # Skips the Camoufox-downstream InvalidIP failures we'd otherwise
    # burn browser time on.
    print(
        f"TCP liveness check ({len(socks)} candidates, 3s timeout)...",
        flush=True,
    )
    alive = await _tcp_filter(socks)
    print(f"  after tcp-alive: {len(alive)}", flush=True)

    print(
        f"Looking up ASNs ({len(alive)} candidates, cached)...",
        flush=True,
    )
    consumer = await filter_by_asn(alive, concurrency=20)
    print(f"  after consumer-ASN filter: {len(consumer)}", flush=True)

    if len(consumer) > max_candidates:
        print(
            f"  capping at {max_candidates} (random sample)",
            flush=True,
        )
        # Random sample for more diverse geographic coverage than the
        # implicit "first N from a github-raw file" order.
        import random
        random.shuffle(consumer)
        consumer = consumer[:max_candidates]
    return consumer


async def _probe_with_persistence(
    proxies: list[ProxyConfig],
    concurrency: int = 3,
) -> tuple[int, int]:
    """Run archive.ph gate probes, persist each outcome as it lands.

    Returns (pass_count, fail_count).
    """
    settings = Settings()
    pool = await create_pool(
        settings.db_url.get_secret_value(), min_size=2, max_size=5
    )
    await init_db(pool)
    repo = ProxyStatusRepository()

    sem = asyncio.Semaphore(concurrency)
    passes = 0
    fails = 0
    started_at = time.perf_counter()

    async def _one(idx: int, proxy: ProxyConfig) -> None:
        nonlocal passes, fails
        async with sem:
            t0 = time.perf_counter()
            ok = await probe_archive_gate(proxy)
            dt = time.perf_counter() - t0
        if ok:
            passes += 1
        else:
            fails += 1
        async with pool.acquire() as conn:
            await repo.record(conn, proxy.server, gate_passing=ok)
        elapsed = time.perf_counter() - started_at
        rate = (passes + fails) / max(elapsed, 0.001)
        eta_s = (len(proxies) - (passes + fails)) / max(rate, 0.001)
        mark = "PASS" if ok else "fail"
        print(
            f"[{idx:>3}/{len(proxies)}] {mark} {dt:5.1f}s  "
            f"{proxy.server}  "
            f"(pass={passes} fail={fails} eta={eta_s / 60:.1f}m)",
            flush=True,
        )

    try:
        await asyncio.gather(
            *(_one(i + 1, p) for i, p in enumerate(proxies))
        )
    finally:
        await close_pool(pool)

    return passes, fails


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=100,
        help="Cap on the number of post-filter proxies to gate-probe "
             "(default: 100)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Parallel Camoufox gate probes (default: 3)",
    )
    args = parser.parse_args()

    settings = Settings()
    if not settings.proxy_list_urls and not settings.proxy_list:
        print(
            "No proxy sources configured — "
            "set ARCHIVER_PROXY_LIST or ARCHIVER_PROXY_LIST_URLS",
            file=sys.stderr,
        )
        return 1

    candidates = await _load_and_prefilter(settings, args.max_candidates)
    if not candidates:
        print("No candidates to probe after filters.", file=sys.stderr)
        return 1

    print(
        f"\nProbing {len(candidates)} candidate(s) at "
        f"concurrency={args.concurrency}...",
        flush=True,
    )
    t0 = time.perf_counter()
    passes, fails = await _probe_with_persistence(
        candidates, concurrency=args.concurrency
    )
    elapsed_min = (time.perf_counter() - t0) / 60

    print()
    print("=" * 60)
    print(f"Gate probe complete in {elapsed_min:.1f} min")
    print(f"  candidates:  {len(candidates)}")
    print(f"  passing:     {passes}  ({passes / len(candidates) * 100:.1f}%)")
    print(f"  failing:     {fails}")
    print("=" * 60)
    print("Gate-passers persisted to proxy_status table.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
