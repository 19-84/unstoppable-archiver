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


# Candidate Chromium paths to try for single-file-cli when none is
# explicitly configured. The Playwright base image lays Chromium down
# at a versioned path; we glob to tolerate upgrades.
_CHROMIUM_CANDIDATES: tuple[str, ...] = (
    "/root/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
    "/root/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


@functools.cache
def _discover_chromium_for_cli() -> str | None:
    """Find a Chromium binary single-file-cli can drive.

    Cached — this is stable for the lifetime of the worker process.
    Returns None if nothing found; the caller should surface a helpful
    error rather than try to run the CLI blindly.
    """
    import glob
    for pattern in _CHROMIUM_CANDIDATES:
        for path in sorted(glob.glob(pattern), reverse=True):
            if Path(path).exists():
                return path
    return None


@beartype
async def capture_via_cli(
    url: str,
    cli_path: str = "single-file",
    browser_path: str | None = None,
    timeout: int = 120,
) -> str:
    """Capture a page using single-file-cli as a subprocess.

    Spawns an independent Chromium via the CLI and returns the HTML
    content written to stdout. Last-resort path when in-browser
    strategies (page.evaluate + Xray script-tag) fail — Firefox's
    world-isolation issues don't affect a separate Chromium process.

    Raises CaptureError on non-zero exit, timeout, or if Chromium
    can't be located.
    """
    if browser_path is None:
        browser_path = _discover_chromium_for_cli()
    if browser_path is None:
        raise CaptureError(
            "single-file-cli needs a Chromium binary; none found "
            "in the usual Playwright cache paths. Set "
            "ARCHIVER_SINGLEFILE_CHROMIUM_PATH explicitly."
        )

    # NB: single-file-cli uses yargs, which prints its help message (not
    # an error) and exits 0 when it sees an unknown flag. Pass ONLY
    # flags present in `single-file --help`.
    #   - `--block-scripts` (default true) covers script removal.
    #   - `--remove-unused-styles` (default true).
    #   - Values starting with "-" need separate argv entries (yargs
    #     misparses `--flag=--val` as two flags).
    # User-Agent rotation — never identify as the archiver. The CLI's
    # bundled Chromium sends a `HeadlessChrome/...` UA by default which
    # triggers bot detection on many origins; replace with a current
    # real-browser UA from our rotating pool.
    from archiver import user_agents as _ua
    ua = _ua.pick()
    cmd = [
        cli_path, url,
        "--browser-executable-path", browser_path,
        "--browser-arg", "--no-sandbox",
        "--browser-arg", "--disable-gpu",
        "--browser-arg", "--disable-dev-shm-usage",
        "--user-agent", ua,
        "--dump-content",                      # HTML → stdout
        "--compress-HTML",
        "--load-deferred-images",
        "--load-deferred-images-max-idle-time", "3000",
    ]

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
    html = stdout.decode("utf-8", errors="replace")
    if not html.strip():
        raise CaptureError(
            "single-file-cli returned empty output "
            f"(stderr: {stderr.decode()[:200]})"
        )
    return html


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
def build_options(
    url: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the options dict for singlefile.getPageData().

    `overrides` merges into the defaults — used by the A/B benchmark
    and by future Settings-level tuning without editing this function.
    """
    opts: dict[str, Any] = {
        # Keep iframes — many sites (Guardian's Sourcepoint consent,
        # embedded YouTube, etc.) render meaningful content inside them.
        "removeFrames": False,
        # Scroll to trigger lazy-loaded images before snapshotting.
        "loadDeferredImages": True,
        "loadDeferredImagesMaxIdleTime": 3000,
        # Scripts are stripped — the archive is a dead document by design.
        "removeScripts": True,
        # Preserve display:none elements by default — many sites use
        # them for menus/modals the user reveals via interaction; a
        # faithful archive keeps them. Benchmark shows this is the
        # biggest single lever for file size when we relax it.
        "removeHiddenElements": False,
        # Minify HTML + CSS output. Lossless — same render, smaller file.
        "compressHTML": True,
        "compressCSS": True,
        # Dead-code-eliminate CSS rules that don't match any element in
        # the captured DOM. Lossless for static archives (no JS runs to
        # add new matching elements after capture).
        "removeUnusedStyles": True,
        "url": url,
    }
    if overrides:
        opts.update(overrides)
    return opts


SINGLEFILE_CAPTURE_JS = """
async (options) => {
    const result = await singlefile.getPageData(options);
    return { content: result.content, title: result.title };
}
"""
