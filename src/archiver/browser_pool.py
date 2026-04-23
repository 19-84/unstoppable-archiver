# ABOUTME: Browser lifecycle management with lazy initialization
# ABOUTME: Manages Playwright Chromium and Camoufox Firefox instances for capture tiers
"""Browser pool for managing Playwright and Camoufox browser instances."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from beartype import beartype
from playwright.async_api import Browser, Playwright, async_playwright

from archiver.config import Settings
from archiver.enums import CaptureTier

log = structlog.get_logger()

# Chromium launch flags: survive without an X server in containers.
# `--use-gl=swiftshader` falls back to a software GL driver when ANGLE/EGL
# cannot initialize (e.g. worker containers without Xvfb). Without these,
# heavy pages (Wikipedia WWII, full-page screenshots) crash the renderer
# with "Target crashed" / "Target page, context or browser has been closed".
_CHROMIUM_LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-gpu",
    "--use-gl=swiftshader",
    "--disable-software-rasterizer",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-features=VizDisplayCompositor",
)


class BrowserPool:
    """Manages long-lived browser instances.

    Lazily creates one Chromium and one optional Camoufox browser.
    Each capture job gets its own BrowserContext + Page (disposed
    after capture). The pool holds the long-lived Browser objects.

    A `disconnected` handler clears the cached reference when a browser
    crashes, so the next get_browser() call relaunches a fresh instance
    instead of returning a dead reference.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._playwright: Playwright | None = None
        self._chromium: Browser | None = None
        self._camoufox: Browser | None = None
        self._camoufox_ctx: Any = None
        self._lock = asyncio.Lock()

    @beartype
    async def get_browser(self, tier: CaptureTier) -> Browser:
        """Return a browser instance for the given tier.

        If the cached browser has disconnected (crashed), the next call
        auto-relaunches it.
        """
        async with self._lock:
            if tier == CaptureTier.CHROMIUM:
                return await self._ensure_chromium()
            return await self._ensure_camoufox()

    def _on_chromium_disconnected(self) -> None:
        """Clear cached Chromium reference when the browser disconnects."""
        log.warning("browser_pool.chromium_disconnected")
        self._chromium = None

    def _on_camoufox_disconnected(self) -> None:
        """Clear cached Camoufox reference when the browser disconnects."""
        log.warning("browser_pool.camoufox_disconnected")
        self._camoufox = None
        self._camoufox_ctx = None

    async def _ensure_chromium(self) -> Browser:
        """Lazily create and return the Chromium browser."""
        if self._chromium is not None and not self._chromium.is_connected():
            log.warning("browser_pool.chromium_stale_reference_cleared")
            self._chromium = None
        if self._chromium is None:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._chromium = await self._playwright.chromium.launch(
                headless=self._settings.chromium_headless,
                args=list(_CHROMIUM_LAUNCH_ARGS),
            )
            self._chromium.on(
                "disconnected",
                lambda _b: self._on_chromium_disconnected(),
            )
            log.info("browser_pool.chromium_launched")
        return self._chromium

    async def _ensure_camoufox(self) -> Browser:
        """Lazily create and return the Camoufox browser."""
        if self._camoufox is not None and not self._camoufox.is_connected():
            log.warning("browser_pool.camoufox_stale_reference_cleared")
            self._camoufox = None
            self._camoufox_ctx = None
        if self._camoufox is None:
            from camoufox.async_api import AsyncCamoufox

            self._camoufox_ctx = AsyncCamoufox(
                headless=self._settings.camoufox_headless,
                humanize=True,
                geoip=True,
            )
            # AsyncCamoufox.__aenter__ returns a Playwright Browser
            browser: Browser = await self._camoufox_ctx.__aenter__()
            self._camoufox = browser
            self._camoufox.on(
                "disconnected",
                lambda _b: self._on_camoufox_disconnected(),
            )
            log.info("browser_pool.camoufox_launched")
        return self._camoufox

    @beartype
    async def close(self) -> None:
        """Shut down all browsers and Playwright."""
        if self._camoufox_ctx is not None:
            await self._camoufox_ctx.__aexit__(None, None, None)
            self._camoufox = None
            self._camoufox_ctx = None
            log.info("browser_pool.camoufox_closed")
        if self._chromium is not None:
            await self._chromium.close()
            self._chromium = None
            log.info("browser_pool.chromium_closed")
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
            log.info("browser_pool.playwright_stopped")
