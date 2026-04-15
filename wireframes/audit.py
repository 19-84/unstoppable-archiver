"""Walk through Glass Noir wireframes, screenshot each page at desktop + mobile."""
import asyncio
from playwright.async_api import async_playwright

BASE = "http://localhost:8888/noir"
PAGES = [
    ("home", "home.html"),
    ("detail", "detail.html"),
    ("capturing", "capturing.html"),
    ("failed", "failed.html"),
    ("viewer", "viewer.html"),
    ("search", "search.html"),
    ("empty", "empty.html"),
    ("errors", "errors.html"),
]
OUT = "screenshots"

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # Desktop
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await ctx.new_page()
        for name, path in PAGES:
            await page.goto(f"{BASE}/{path}", wait_until="networkidle")
            await page.screenshot(
                path=f"{OUT}/{name}-desktop.png",
                full_page=True,
            )
            print(f"  desktop: {name}")
        await ctx.close()

        # Mobile
        ctx = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await ctx.new_page()
        for name, path in PAGES:
            await page.goto(f"{BASE}/{path}", wait_until="networkidle")
            await page.screenshot(
                path=f"{OUT}/{name}-mobile.png",
                full_page=True,
            )
            print(f"  mobile:  {name}")
        await ctx.close()

        await browser.close()

asyncio.run(main())
