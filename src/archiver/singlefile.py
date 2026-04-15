# ABOUTME: SingleFile integration — CLI subprocess (preferred) and JS bundle fallback
# ABOUTME: Provides self-contained HTML snapshot capture via single-file-cli or page.evaluate()
"""SingleFile integration for self-contained HTML snapshots."""

from __future__ import annotations

import asyncio
import functools
import shutil
from pathlib import Path
from typing import Any

import structlog
from beartype import beartype

from archiver.errors import CaptureError

log = structlog.get_logger()


@beartype
async def capture_via_cli(
    url: str,
    cli_path: str = "single-file",
    browser_path: str | None = None,
    timeout: int = 120,
) -> str:
    """Capture a page using single-file-cli as a subprocess.

    Returns the HTML content as a string. Falls back to bundle
    injection if the CLI is not installed.
    """
    cmd = [
        cli_path, url,
        "--dump-content",
        "--compress-HTML",
        "--remove-scripts",
        "--load-deferred-images",
        "--load-deferred-images-max-idle-time", "3000",
        "--remove-unused-styles",
    ]
    if browser_path:
        cmd.extend(["--browser-executable-path", browser_path])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except TimeoutError:
        proc.kill()
        raise CaptureError(f"single-file-cli timed out after {timeout}s") from None

    if proc.returncode != 0:
        raise CaptureError(
            f"single-file-cli exit {proc.returncode}: {stderr.decode()[:500]}"
        )
    return stdout.decode("utf-8")


@beartype
def cli_available(cli_path: str = "single-file") -> bool:
    """Check if single-file-cli is installed."""
    return shutil.which(cli_path) is not None


# --- Legacy JS bundle approach (used when CLI unavailable or for Firefox) ---


@functools.cache
def _load_bundle_cached(bundle_path_str: str) -> str:
    """Load and cache the SingleFile JS bundle. Uses str key for hashability."""
    content = Path(bundle_path_str).read_text(encoding="utf-8")
    # The npm bundle is an ES module: const script = "..."; export {...};
    # Strip the export statement and eval the script string to define
    # the `singlefile` global that capture code depends on.
    if content.startswith("const script = "):
        content = content.split("export {")[0].strip() + "\n(0, eval)(script);"
    log.info(
        "singlefile.bundle_loaded",
        path=bundle_path_str,
        size=len(content),
    )
    return content


@beartype
def load_bundle(bundle_path: Path) -> str:
    """Load the SingleFile JS bundle from disk, caching in memory."""
    return _load_bundle_cached(str(bundle_path.resolve()))


@beartype
def build_options(url: str) -> dict[str, Any]:
    """Build the options dict for singlefile.getPageData()."""
    return {
        "removeFrames": False,
        "loadDeferredImages": True,
        "loadDeferredImagesMaxIdleTime": 3000,
        "removeScripts": True,
        "removeHiddenElements": False,
        "compressHTML": True,
        "removeUnusedStyles": True,
        "url": url,
    }


SINGLEFILE_CAPTURE_JS = """
async (options) => {
    const result = await singlefile.getPageData(options);
    return { content: result.content, title: result.title };
}
"""
