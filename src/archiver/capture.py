# ABOUTME: Core capture pipeline — one browser session produces all output formats
# ABOUTME: Orchestrates SingleFile injection, WARC writing, screenshot, and text extraction
# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false
"""Core capture pipeline for archiving web pages."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from beartype import beartype
from PIL import Image
from playwright.async_api import Browser, Response

from archiver.detection import check_anti_bot
from archiver.errors import AntiBotDetectedError, CaptureError
from archiver.models import CaptureResult
from archiver.singlefile import (
    SINGLEFILE_CAPTURE_JS,
    build_options,
    load_bundle,
)
from archiver.warc_writer import CapturedExchange, PlaywrightWARCWriter

if TYPE_CHECKING:
    from archiver.config import Settings

log = structlog.get_logger()


@beartype
async def capture_page(
    url: str,
    browser: Browser,
    settings: Settings,
) -> CaptureResult:
    """Capture a page: SingleFile HTML + WARC + screenshot + text.

    All outputs come from a single browser session. Raises
    AntiBotDetectedError if the page appears to be blocked.
    Raises CaptureError for other failures.
    """
    warc_writer = PlaywrightWARCWriter()
    bundle_js = load_bundle(settings.singlefile_bundle_path)

    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        java_script_enabled=True,
    )
    page = await context.new_page()

    try:
        # Collect HTTP exchanges for WARC
        async def _on_response(response: Response) -> None:
            try:
                body = await response.body()
                req = response.request
                warc_writer.add_exchange(
                    CapturedExchange(
                        url=response.url,
                        method=req.method,
                        request_headers=dict(
                            await req.all_headers()
                        ),
                        status=response.status,
                        response_headers=dict(
                            await response.all_headers()
                        ),
                        body=body,
                    )
                )
            except Exception:
                log.debug("warc.response_skip", url=response.url)

        page.on("response", _on_response)

        # Inject SingleFile bundle before navigation
        await page.add_init_script(script=bundle_js)

        # Navigate
        response = await page.goto(
            url,
            wait_until="networkidle",
            timeout=settings.max_capture_timeout * 1000,
        )

        status_code = response.status if response else 0
        page_title = await page.title()
        body_text = await page.evaluate(
            "document.body ? document.body.innerText : ''"
        )

        # Check for anti-bot blocking
        signal = check_anti_bot(status_code, page_title, body_text)
        if signal.is_blocked:
            raise AntiBotDetectedError(
                f"Blocked: {signal.reason}"
            )

        # Capture SingleFile snapshot
        sf_options = build_options(url)
        sf_result = await page.evaluate(
            SINGLEFILE_CAPTURE_JS, sf_options
        )
        if not isinstance(sf_result, dict) or "content" not in sf_result:
            raise CaptureError(
                "SingleFile returned unexpected result: "
                + repr(type(sf_result))
            )
        snapshot_html = sf_result["content"].encode("utf-8")
        title = sf_result.get("title") or page_title

        # Screenshot
        screenshot_png = await page.screenshot(full_page=True)

        # Text extraction
        text_content: str = await page.evaluate(
            "document.body ? document.body.innerText : ''"
        )

        # Generate thumbnail
        thumbnail_png = _generate_thumbnail(
            screenshot_png,
            settings.thumbnail_width,
            settings.thumbnail_height,
        )

        # Write WARC
        warc_path = None
        warc_size = 0
        if warc_writer.exchange_count > 0:
            warc_path = (
                settings.artifacts_dir / f"tmp_{uuid.uuid4().hex}.warc.gz"
            )
            warc_size = warc_writer.finalize(warc_path)

        # Content hashes for dedup
        content_hash = hashlib.sha256(snapshot_html).hexdigest()
        screenshot_hash = hashlib.sha256(
            screenshot_png
        ).hexdigest()

        return CaptureResult(
            snapshot_html=snapshot_html,
            screenshot_png=screenshot_png,
            thumbnail_png=thumbnail_png,
            text_content=text_content,
            title=title,
            warc_path=warc_path,
            warc_size=warc_size,
            content_hash=content_hash,
            screenshot_hash=screenshot_hash,
        )

    except AntiBotDetectedError:
        raise
    except Exception as exc:
        raise CaptureError(f"Capture failed: {exc}") from exc
    finally:
        await context.close()


@beartype
def _generate_thumbnail(
    screenshot_png: bytes,
    width: int,
    height: int,
) -> bytes:
    """Resize screenshot to thumbnail dimensions."""
    img = Image.open(BytesIO(screenshot_png))
    img.thumbnail((width, height), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@beartype
async def save_artifacts(
    result: CaptureResult,
    url_hash: str,
    artifacts_dir: Path,
) -> str:
    """Save all capture artifacts to disk.

    Returns the relative artifact directory path.
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    rel_dir = f"{url_hash}/{timestamp}"
    out_dir = artifacts_dir / rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "snapshot.html").write_bytes(result.snapshot_html)
    (out_dir / "screenshot.png").write_bytes(result.screenshot_png)
    (out_dir / "thumbnail.png").write_bytes(result.thumbnail_png)

    if result.warc_path and result.warc_path.exists():
        result.warc_path.rename(out_dir / "archive.warc.gz")

    log.info(
        "artifacts.saved",
        dir=rel_dir,
        snapshot_size=len(result.snapshot_html),
        warc_size=result.warc_size,
    )
    return rel_dir
