#!/usr/bin/env python3
# ABOUTME: A/B benchmark for SingleFile options — measure size/fidelity tradeoff
# ABOUTME: Usage: uv run python scripts/bench_singlefile_options.py <url>
"""Capture a URL multiple times with different SingleFile option sets.

For each option set:
  1. Run the standard capture pipeline with the override merged in.
  2. Measure the resulting snapshot.html size.
  3. Render the snapshot in a fresh browser and screenshot it.
  4. Compare the screenshot to the baseline (first variant) via
     perceptual pixel diff.

Emits a table showing size savings vs. visual fidelity loss per option,
so we can pick settings that preserve parity where it matters.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

# Variants to test. The first entry is the baseline. Each subsequent
# variant is baseline PLUS the override.
VARIANTS: list[tuple[str, dict[str, Any]]] = [
    ("baseline", {}),
    ("+removeHiddenElements", {"removeHiddenElements": True}),
    (
        "+maxResourceSize=500KB",
        {"maxResourceSizeEnabled": True, "maxResourceSize": 0.5},
    ),
    (
        "+maxResourceSize=200KB",
        {"maxResourceSizeEnabled": True, "maxResourceSize": 0.2},
    ),
    (
        "+maxResourceSize=200KB +removeHiddenElements",
        {
            "maxResourceSizeEnabled": True,
            "maxResourceSize": 0.2,
            "removeHiddenElements": True,
        },
    ),
]

OUT_DIR = Path("/tmp/bench_singlefile")  # noqa: S108


async def capture_once(
    url: str,
    overrides: dict[str, Any],
    label: str,
) -> Path:
    """Run the capture pipeline once and return the saved snapshot path."""
    from archiver.browser_pool import BrowserPool
    from archiver.capture import capture_page
    from archiver.config import Settings
    from archiver.singlefile import build_options

    settings = Settings(artifacts_dir=OUT_DIR / "raw")

    pool = BrowserPool(settings)
    browser = await pool.get_browser(
        __import__("archiver.enums", fromlist=["CaptureTier"]).CaptureTier.CHROMIUM
    )
    # Monkey-patch build_options to inject overrides for this run. We do
    # it through a temporary wrapper so the benchmark doesn't have to
    # touch the capture function signature.
    import archiver.capture as capture_mod

    original = capture_mod.build_options

    def patched(u: str) -> dict[str, Any]:
        return build_options(u, overrides=overrides)

    capture_mod.build_options = patched
    try:
        result = await capture_page(url, browser, settings)
    finally:
        capture_mod.build_options = original
        await pool.close()

    safe_label = label.replace(" ", "_").replace("+", "p").replace("=", "eq")
    out = OUT_DIR / f"snapshot_{safe_label}.html"
    out.write_bytes(result.snapshot_html)
    return out


async def render_and_screenshot(
    snapshot: Path, out_png: Path
) -> None:
    """Render a local snapshot HTML file and take a full-page screenshot."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--use-gl=swiftshader",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1920, "height": 1080}
        )
        page = await ctx.new_page()
        await page.goto(
            snapshot.absolute().as_uri(),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=10000)
        await page.screenshot(path=str(out_png), full_page=True)
        await browser.close()


def screenshot_similarity(a: Path, b: Path) -> float:
    """Return a 0..1 perceptual similarity score.

    Uses simple downsample + normalized histogram correlation — good
    enough to tell "visually identical" from "missing the hero image".
    """
    from PIL import Image

    img_a = Image.open(a).convert("RGB").resize((256, 256))
    img_b = Image.open(b).convert("RGB").resize((256, 256))

    # Compute per-channel histograms, flatten, correlate.
    hist_a = img_a.histogram()
    hist_b = img_b.histogram()
    if len(hist_a) != len(hist_b) or not any(hist_a) or not any(hist_b):
        return 0.0
    # Simple cosine similarity.
    dot = sum(x * y for x, y in zip(hist_a, hist_b, strict=True))
    mag_a = sum(x * x for x in hist_a) ** 0.5
    mag_b = sum(x * x for x in hist_b) ** 0.5
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


async def main(url: str) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "raw").mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    baseline_png: Path | None = None

    for label, overrides in VARIANTS:
        print(f"=== capturing: {label} ===", flush=True)
        snapshot = await capture_once(url, overrides, label)
        size_bytes = snapshot.stat().st_size
        size_mb = size_bytes / 1024 / 1024

        png = OUT_DIR / f"render_{snapshot.stem.replace('snapshot_', '')}.png"
        await render_and_screenshot(snapshot, png)

        similarity = 1.0
        if baseline_png is None:
            baseline_png = png
        else:
            similarity = screenshot_similarity(baseline_png, png)

        results.append({
            "label": label,
            "snapshot_size_mb": round(size_mb, 2),
            "render_png": str(png),
            "similarity_to_baseline": round(similarity, 4),
        })
        print(
            f"  size={size_mb:.1f} MB  "
            f"similarity_to_baseline={similarity:.4f}",
            flush=True,
        )

    # Summary table.
    print()
    print(f"{'Variant':<40} {'Size (MB)':>10} {'Δ vs base':>10} {'Sim':>8}")
    print("-" * 72)
    baseline_size = results[0]["snapshot_size_mb"]
    for r in results:
        delta = r["snapshot_size_mb"] - baseline_size
        pct = (delta / baseline_size * 100) if baseline_size else 0
        print(
            f"{r['label']:<40} "
            f"{r['snapshot_size_mb']:>10.2f} "
            f"{pct:>+9.1f}% "
            f"{r['similarity_to_baseline']:>8.4f}"
        )

    digest = hashlib.sha256(url.encode()).hexdigest()[:8]
    json_out = OUT_DIR / f"results_{digest}.json"
    json_out.write_text(json.dumps(results, indent=2))
    print(f"\nDetails: {json_out}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:  # noqa: PLR2004
        print("usage: bench_singlefile_options.py <url>", file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1])))
