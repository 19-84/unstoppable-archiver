#!/usr/bin/env python3
# ABOUTME: One-shot backfill — compress existing snapshot.html → snapshot.html.zst
# ABOUTME: Idempotent: skips dirs already compressed, leaves originals if compress fails
"""Backfill existing archives to zstd-compressed snapshot.html.zst.

Walks `<artifacts_dir>/<url-hash>/<timestamp>/` directories, compresses
each `snapshot.html` to `snapshot.html.zst` (zstd level 19), then
deletes the original on success. The capture pipeline writes new
archives in the compressed form directly, so this only needs to run
once against historical data.

**Stop the worker before running** to avoid racing the writer; if the
worker is up it might be writing `snapshot.html` to a fresh dir while
this script is mid-compress. The script does NOT attempt locking — a
maintenance window is the correct approach.

Run:
    docker compose stop worker
    docker compose run --rm app uv run python scripts/compress_snapshots.py
    docker compose start worker

Flags:
    --dry-run      Walk + report sizes, write nothing.
    --root PATH    Override artifacts_dir (defaults to settings).
    --level N      zstd compression level (default 19, matches capture).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import zstandard as zstd  # type: ignore[import-not-found]

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from archiver.config import Settings

_DEFAULT_LEVEL = 19


def _compress_one(
    plain: Path, level: int, dry_run: bool,
) -> tuple[int, int]:
    """Compress one snapshot.html. Returns (orig_size, zst_size).

    Skips entirely if a sibling .zst already exists. Leaves the
    original in place if compression fails or the result is
    suspiciously empty.
    """
    zst = plain.with_suffix(".html.zst")
    if zst.exists():
        return (0, 0)  # already done; treat as no-op for stats

    orig = plain.read_bytes()
    if not orig:
        return (0, 0)

    compressor = zstd.ZstdCompressor(level=level)
    compressed = compressor.compress(orig)
    if not compressed:
        # zstd would never return empty on non-empty input but guard
        # against pathological encoders just in case.
        raise RuntimeError(f"empty compressor output for {plain}")

    if dry_run:
        return (len(orig), len(compressed))

    # Write to a temp name first so a crash mid-write doesn't leave
    # a half-written .zst that compress_one would skip next time.
    tmp = zst.with_suffix(".zst.tmp")
    tmp.write_bytes(compressed)
    tmp.rename(zst)
    plain.unlink()
    return (len(orig), len(compressed))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--level", type=int, default=_DEFAULT_LEVEL)
    args = parser.parse_args()

    root: Path = args.root or Settings().artifacts_dir
    if not root.exists():
        print(f"artifacts_dir does not exist: {root}", file=sys.stderr)
        return 2

    print(f"scanning {root} ...")
    total_orig = 0
    total_zst = 0
    converted = 0
    skipped = 0
    failed = 0

    # snapshot.html layout: <root>/<url_hash>/<timestamp>/snapshot.html
    for plain in root.rglob("snapshot.html"):
        try:
            orig, zst = _compress_one(plain, args.level, args.dry_run)
            if orig == 0:
                skipped += 1
                continue
            total_orig += orig
            total_zst += zst
            converted += 1
            if converted % 100 == 0:
                pct = (1 - total_zst / total_orig) * 100 if total_orig else 0
                print(
                    f"  {converted} done, "
                    f"{total_orig / 1e9:.2f} GB -> "
                    f"{total_zst / 1e9:.2f} GB ({pct:.1f}% saved)",
                )
        except Exception as exc:
            failed += 1
            print(f"  FAIL {plain}: {exc}", file=sys.stderr)

    print()
    print("=== Summary ===")
    print(f"  converted: {converted}")
    print(f"  skipped (already .zst): {skipped}")
    print(f"  failed: {failed}")
    if total_orig:
        pct = (1 - total_zst / total_orig) * 100
        print(
            f"  bytes: {total_orig / 1e9:.2f} GB -> "
            f"{total_zst / 1e9:.2f} GB ({pct:.1f}% saved)",
        )
    if args.dry_run:
        print("  (dry run — nothing was written)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
