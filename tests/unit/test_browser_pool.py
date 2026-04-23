# ABOUTME: Unit tests for BrowserPool lifecycle with mocked Playwright/Camoufox
# ABOUTME: Tests lazy init, singleton behavior, tier routing, and cleanup
"""Tests for browser pool management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import Browser

from archiver.browser_pool import BrowserPool
from archiver.config import Settings
from archiver.enums import CaptureTier


def _make_settings() -> Settings:
    return Settings(
        chromium_headless=True, camoufox_headless="virtual"
    )


def _mock_browser() -> MagicMock:
    """Create a mock that passes beartype's isinstance check."""
    return MagicMock(spec=Browser)


class TestBrowserPoolChromium:
    @patch("archiver.browser_pool.async_playwright")
    async def test_get_browser_chromium(
        self, mock_pw_fn: MagicMock
    ) -> None:
        mock_browser = _mock_browser()
        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(
            return_value=mock_browser
        )
        mock_pw_fn.return_value.start = AsyncMock(
            return_value=mock_pw
        )

        pool = BrowserPool(_make_settings())
        browser = await pool.get_browser(CaptureTier.CHROMIUM)

        assert browser is mock_browser

    @patch("archiver.browser_pool.async_playwright")
    async def test_chromium_lazy_singleton(
        self, mock_pw_fn: MagicMock
    ) -> None:
        mock_browser = _mock_browser()
        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(
            return_value=mock_browser
        )
        mock_pw_fn.return_value.start = AsyncMock(
            return_value=mock_pw
        )

        pool = BrowserPool(_make_settings())
        b1 = await pool.get_browser(CaptureTier.CHROMIUM)
        b2 = await pool.get_browser(CaptureTier.CHROMIUM)

        assert b1 is b2
        mock_pw.chromium.launch.assert_awaited_once()


class TestBrowserPoolCamoufox:
    @patch("archiver.browser_pool.async_playwright")
    async def test_get_browser_camoufox(
        self, mock_pw_fn: MagicMock
    ) -> None:
        mock_browser = _mock_browser()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(
            return_value=mock_browser
        )

        pool = BrowserPool(_make_settings())

        with patch(
            "camoufox.async_api.AsyncCamoufox",
            return_value=mock_ctx,
        ):
            browser = await pool.get_browser(
                CaptureTier.CAMOUFOX
            )
            assert browser is mock_browser

    @patch("archiver.browser_pool.async_playwright")
    async def test_non_chromium_routes_to_camoufox(
        self, mock_pw_fn: MagicMock
    ) -> None:
        mock_browser = _mock_browser()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(
            return_value=mock_browser
        )

        pool = BrowserPool(_make_settings())

        with patch(
            "camoufox.async_api.AsyncCamoufox",
            return_value=mock_ctx,
        ):
            browser = await pool.get_browser(
                CaptureTier.CAMOUFOX_PROXY
            )
            assert browser is mock_browser


class TestBrowserPoolClose:
    @patch("archiver.browser_pool.async_playwright")
    async def test_close_all(
        self, mock_pw_fn: MagicMock
    ) -> None:
        mock_chromium = _mock_browser()
        mock_chromium.close = AsyncMock()
        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(
            return_value=mock_chromium
        )
        mock_pw_fn.return_value.start = AsyncMock(
            return_value=mock_pw
        )

        mock_camoufox = _mock_browser()
        mock_camoufox_ctx = AsyncMock()
        mock_camoufox_ctx.__aenter__ = AsyncMock(
            return_value=mock_camoufox
        )
        mock_camoufox_ctx.__aexit__ = AsyncMock()

        pool = BrowserPool(_make_settings())
        await pool.get_browser(CaptureTier.CHROMIUM)

        with patch(
            "camoufox.async_api.AsyncCamoufox",
            return_value=mock_camoufox_ctx,
        ):
            await pool.get_browser(CaptureTier.CAMOUFOX)

        await pool.close()

        mock_camoufox_ctx.__aexit__.assert_awaited_once()
        mock_chromium.close.assert_awaited_once()
        mock_pw.stop.assert_awaited_once()

    async def test_close_when_nothing_initialized(
        self,
    ) -> None:
        pool = BrowserPool(_make_settings())
        await pool.close()


class TestBrowserPoolDisconnectHandlers:
    def test_chromium_disconnected_clears_ref(self) -> None:
        pool = BrowserPool(_make_settings())
        pool._chromium = _mock_browser()
        pool._on_chromium_disconnected()
        assert pool._chromium is None

    def test_camoufox_disconnected_clears_refs(self) -> None:
        pool = BrowserPool(_make_settings())
        pool._camoufox = _mock_browser()
        pool._camoufox_ctx = AsyncMock()
        pool._on_camoufox_disconnected()
        assert pool._camoufox is None
        assert pool._camoufox_ctx is None

    @patch("archiver.browser_pool.async_playwright")
    async def test_stale_chromium_relaunches(
        self, mock_pw_fn: MagicMock
    ) -> None:
        """A disconnected cached browser must be replaced, not returned."""
        stale = _mock_browser()
        stale.is_connected = MagicMock(return_value=False)
        fresh = _mock_browser()
        fresh.is_connected = MagicMock(return_value=True)
        mock_pw = AsyncMock()
        mock_pw.chromium.launch = AsyncMock(return_value=fresh)
        mock_pw_fn.return_value.start = AsyncMock(return_value=mock_pw)

        pool = BrowserPool(_make_settings())
        pool._chromium = stale  # simulate a prior launch that died
        browser = await pool.get_browser(CaptureTier.CHROMIUM)
        assert browser is fresh

    async def test_stale_camoufox_relaunches(self) -> None:
        stale = _mock_browser()
        stale.is_connected = MagicMock(return_value=False)
        fresh = _mock_browser()
        fresh.is_connected = MagicMock(return_value=True)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=fresh)

        pool = BrowserPool(_make_settings())
        pool._camoufox = stale
        pool._camoufox_ctx = AsyncMock()
        with patch(
            "camoufox.async_api.AsyncCamoufox", return_value=mock_ctx
        ):
            browser = await pool.get_browser(CaptureTier.CAMOUFOX)
        assert browser is fresh
