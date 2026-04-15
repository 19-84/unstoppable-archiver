"""Run Lighthouse via Playwright on each Glass Noir page."""
import asyncio
import json
import subprocess
from pathlib import Path

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
OUT = Path("screenshots")

def run_lighthouse(name: str, url: str) -> dict:
    """Run Lighthouse CLI and return scores."""
    out_file = OUT / f"{name}-lighthouse.json"
    cmd = [
        "npx", "lighthouse", url,
        "--output=json",
        f"--output-path={out_file}",
        "--chrome-flags=--headless --no-sandbox --disable-gpu",
        "--only-categories=performance,accessibility,best-practices,seo",
        "--quiet",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"  WARN {name}: {result.stderr[:200]}")

    if out_file.exists():
        data = json.loads(out_file.read_text())
        cats = data.get("categories", {})
        return {
            k: round(v.get("score", 0) * 100)
            for k, v in cats.items()
        }
    return {}

def main():
    print("Running Lighthouse on Glass Noir pages...\n")
    print(f"{'Page':<15} {'Perf':>5} {'A11y':>5} {'BP':>5} {'SEO':>5}")
    print("-" * 40)

    for name, path in PAGES:
        url = f"{BASE}/{path}"
        scores = run_lighthouse(name, url)
        if scores:
            print(f"{name:<15} {scores.get('performance', '?'):>5} {scores.get('accessibility', '?'):>5} {scores.get('best-practices', '?'):>5} {scores.get('seo', '?'):>5}")
        else:
            print(f"{name:<15}   FAILED")

if __name__ == "__main__":
    main()
