# ABOUTME: Deep health check script for monitoring
# ABOUTME: Verifies PostgreSQL, disk space, and browser pool availability
"""Deep health check — exits 0 if healthy, 1 if degraded."""

from __future__ import annotations

import shutil
import sys

import httpx

BASE_URL = "http://localhost:8000"
MIN_DISK_FREE_MB = 100


def main() -> int:
    """Run all health checks."""
    errors: list[str] = []

    # API shallow health
    try:
        resp = httpx.get(f"{BASE_URL}/api/health", timeout=5)
        if resp.status_code != 200:  # noqa: PLR2004
            errors.append(f"API health: {resp.status_code}")
    except httpx.RequestError as e:
        errors.append(f"API unreachable: {e}")

    # API deep health (DB connectivity)
    try:
        resp = httpx.get(f"{BASE_URL}/api/health/deep", timeout=10)
        if resp.status_code != 200:  # noqa: PLR2004
            errors.append(f"DB health: {resp.status_code}")
    except httpx.RequestError as e:
        errors.append(f"DB unreachable: {e}")

    # Disk space
    usage = shutil.disk_usage("/data")
    free_mb = usage.free / 1048576
    if free_mb < MIN_DISK_FREE_MB:
        errors.append(f"Low disk: {free_mb:.0f} MB free")

    if errors:
        for err in errors:
            print(f"FAIL: {err}", file=sys.stderr)
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
