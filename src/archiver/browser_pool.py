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


class BrowserPool:
    """Manages long-lived browser instances.

    Lazily creates one Chromium and one optional Camoufox browser.
    Each capture job gets its own BrowserContext + Page (disposed
    after capture). The pool holds the long-lived Browser objects.
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
        """Return a browser instance for the given tier."""
        async with self._lock:
            if tier == CaptureTier.CHROMIUM:
                return await self._ensure_chromium()
            return await self._ensure_camoufox()

    async def _ensure_chromium(self) -> Browser:
        """Lazily create and return the Chromium browser."""
        if self._chromium is None:
            self._playwright = await async_playwright().start()
            self._chromium = await self._playwright.chromium.launch(
                headless=self._settings.chromium_headless,
            )
            log.info("browser_pool.chromium_launched")
        return self._chromium

    async def _ensure_camoufox(self) -> Browser:
        """Lazily create and return the Camoufox browser."""
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
