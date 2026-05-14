#!/usr/bin/env python3
# ABOUTME: Import existing .warc.gz files — extracts HTML responses, creates archives
# ABOUTME: Usage: uv run python scripts/import_warc.py path/to/file.warc.gz [more.warc.gz ...]
"""Import WARC files into the archiver.

For each HTML response record in the WARC:
  - Creates an archive row (status=complete, source=direct)
  - Copies the WARC file into the archive's artifact_dir
  - Extracts text content for search indexing
  - Generates a thumbnail-sized placeholder PNG

This is an admin CLI; not exposed via HTTP.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger()


async def import_warc_file(warc_path: Path) -> int:  # noqa: PLR0915
    """Import one WARC file. Returns the number of archives created."""
    from warcio.archiveiterator import ArchiveIterator  # type: ignore[import-untyped]

    from archiver.config import Settings
    from archiver.db import create_pool, init_db
    from archiver.enums import ArchiveStatus, CaptureSource, CaptureTier
    from archiver.repository import ArchiveRepository
    from archiver.url import normalize_url, url_hash

    settings = Settings()
    pool = await create_pool(settings.db_url.get_secret_value())
    await init_db(pool)
    archive_repo = ArchiveRepository()
    count = 0

    try:
        # Pass 1: collect HTML responses
        html_records: list[tuple[str, bytes, str]] = []  # (url, html, title)
        with warc_path.open("rb") as f:
            for record in ArchiveIterator(f):
                if record.rec_type != "response":
                    continue
                ctype = record.http_headers.get_header("content-type", "") if record.http_headers else ""
                if "html" not in ctype.lower():
                    continue
                url = record.rec_headers.get_header("WARC-Target-URI")
                if not url or not url.startswith(("http://", "https://")):
                    continue
                try:
                    body = record.content_stream().read()
                except Exception as exc:
                    log.warning(
                        "warc.import.record_read_failed",
                        url=url, error=str(exc)[:120],
                    )
                    continue
                # Try to extract <title>
                title = _extract_title(body)
                html_records.append((url, body, title))

        log.info("warc.import.scan", path=str(warc_path), html_count=len(html_records))

        if not html_records:
            return 0

        # Pass 2: create archives and artifacts
        for url, body, title in html_records:
            async with pool.acquire() as conn:
                uhash = url_hash(url)
                # Skip if already imported (same content hash)
                content_hash = hashlib.sha256(body).hexdigest()
                existing = await conn.fetchrow(
                    "SELECT id FROM archives WHERE url_hash = $1"
                    " AND content_hash = $2",
                    uhash, content_hash,
                )
                if existing:
                    log.info("warc.import.skip_duplicate", url=url)
                    continue

                archive = await archive_repo.create(conn, url)
                ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
                rel_dir = f"{archive.id}/{ts}"
                out_dir = settings.artifacts_dir / rel_dir
                out_dir.mkdir(parents=True, exist_ok=True)

                # Copy WARC file itself to the artifact dir
                shutil.copy2(warc_path, out_dir / "archive.warc.gz")
                # Write the HTML snapshot
                (out_dir / "snapshot.html").write_bytes(body)

                # Rudimentary text extraction (strip tags)
                text = _strip_tags(body.decode("utf-8", errors="replace"))[:50_000]

                # Placeholder 1x1 PNG
                placeholder_png = (
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    b"\x00\x00\x00\x01\x00\x00\x00\x01"
                    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
                    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff"
                    b"\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7"
                    b"\x00\x00\x00\x00IEND\xaeB`\x82"
                )
                (out_dir / "screenshot.png").write_bytes(placeholder_png)
                (out_dir / "thumbnail.png").write_bytes(placeholder_png)

                await archive_repo.update_status(
                    conn,
                    archive.id,
                    ArchiveStatus.COMPLETE,
                    title=title or normalize_url(url),
                    text_content=text,
                    artifact_dir=rel_dir,
                    content_hash=content_hash,
                    screenshot_hash=hashlib.sha256(placeholder_png).hexdigest(),
                    snapshot_size=len(body),
                    warc_size=warc_path.stat().st_size,
                    source=CaptureSource.DIRECT.value,
                    tier=CaptureTier.CHROMIUM.value,
                )
                count += 1
                log.info("warc.import.archived", url=url, archive_id=archive.id)

    finally:
        await pool.close()

    return count


def _extract_title(html: bytes) -> str:
    """Extract <title> tag content from HTML bytes."""
    try:
        text = html.decode("utf-8", errors="replace")
        lo = text.lower()
        start = lo.find("<title")
        if start < 0:
            return ""
        start = lo.find(">", start) + 1
        end = lo.find("</title>", start)
        if end < 0:
            return ""
        return text[start:end].strip()[:500]
    except Exception:
        return ""


def _strip_tags(html: str) -> str:
    """Very rough tag stripping for search indexing."""
    import re
    return re.sub(r"<[^>]+>", " ", html)


async def main() -> int:
    if len(sys.argv) < 2:  # noqa: PLR2004
        print("usage: import_warc.py file.warc.gz [more.warc.gz ...]", file=sys.stderr)
        return 1

    from archiver.logging import setup_logging
    setup_logging("INFO", "console")

    total = 0
    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists():
            print(f"skip missing: {path}", file=sys.stderr)
            continue
        n = await import_warc_file(path)
        total += n

    print(f"Imported {total} archive(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
