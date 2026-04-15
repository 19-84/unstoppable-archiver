# ABOUTME: SingleFile JS bundle loader and injection into Playwright pages
# ABOUTME: Provides self-contained HTML snapshot capture via page.evaluate()
"""SingleFile integration for self-contained HTML snapshots."""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import structlog
from beartype import beartype

log = structlog.get_logger()


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
