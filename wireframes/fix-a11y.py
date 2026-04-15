"""Fix all Lighthouse accessibility issues across Glass Noir wireframes."""
import re
from pathlib import Path

NOIR = Path("noir")

# Common fixes applied to ALL pages
CONTRAST_FIXES = {
    "text-neutral-500": "text-neutral-400",
    "text-neutral-600": "text-neutral-400",
    "text-neutral-700": "text-neutral-400",
    "text-neutral-800": "text-neutral-500",
    "text-neutral-900": "text-neutral-500",
    "placeholder-neutral-700": "placeholder-neutral-500",
    "placeholder-neutral-800": "placeholder-neutral-500",
}

# Don't replace inside class names that are bg- or border-
def fix_contrast(html: str) -> str:
    for old, new in CONTRAST_FIXES.items():
        html = html.replace(old, new)
    return html

def add_meta_description(html: str) -> str:
    if 'meta name="description"' not in html:
        html = html.replace(
            "<title>",
            '<meta name="description" content="Unstoppable Archive — self-hosted web preservation with five capture tiers">\n<title>'
        )
    return html

def add_focus_styles(html: str) -> str:
    focus_css = "*:focus-visible{outline:2px solid #a855f7;outline-offset:2px;border-radius:4px;}"
    if "focus-visible" not in html:
        html = html.replace("</style>", focus_css + "\n</style>")
    return html

def add_lang(html: str) -> str:
    if 'lang="en"' not in html:
        html = html.replace("<html>", '<html lang="en">')
    return html

def fix_logo_link(html: str) -> str:
    # Add aria-label to logo dot links
    html = html.replace(
        '<a href="home.html" class="shrink-0"><div class="w-1.5 h-1.5 bg-purple-400 rounded-full"></div></a>',
        '<a href="home.html" class="shrink-0" aria-label="Home"><div class="w-1.5 h-1.5 bg-purple-400 rounded-full" aria-hidden="true"></div></a>'
    )
    html = html.replace(
        '<a href="home.html">\n      <div class="w-1.5 h-1.5 bg-purple-400 rounded-full"></div>\n    </a>',
        '<a href="home.html" aria-label="Home"><div class="w-1.5 h-1.5 bg-purple-400 rounded-full" aria-hidden="true"></div></a>'
    )
    return html

def fix_inputs(html: str) -> str:
    # Add sr-only labels before unlabeled inputs
    if '<label' not in html and '<input' in html:
        # Search inputs
        html = re.sub(
            r'(<input type="text"[^>]*value="[^"]*")',
            r'<label for="search-input" class="sr-only">Search</label>\n      \1 id="search-input"',
            html,
            count=1,
        )
        html = re.sub(
            r'(<input type="url"[^>]*placeholder="https://")',
            r'<label for="url-input" class="sr-only">URL to archive</label>\n      \1 id="url-input"',
            html,
            count=1,
        )
        # Any remaining unlabeled search input
        html = re.sub(
            r'(<input type="text"[^>]*placeholder="[^"]*"[^>]*>)',
            lambda m: m.group(0) if 'id=' in m.group(0) else m.group(0).replace('<input ', '<input aria-label="Search" '),
            html,
        )
    return html

def fix_select(html: str) -> str:
    html = html.replace(
        '<select class=',
        '<select aria-label="Select snapshot version" class='
    )
    return html

def add_main_landmark(html: str) -> str:
    if '<main' not in html:
        html = html.replace('<article ', '<main role="main"><article ')
        html = html.replace('</article>', '</article></main>')
    return html

def add_aria_hidden_decorative(html: str) -> str:
    # Mark decorative background divs
    html = html.replace(
        'class="fixed inset-0 pointer-events-none">',
        'class="fixed inset-0 pointer-events-none" aria-hidden="true">'
    )
    return html

def add_sr_only_class(html: str) -> str:
    if "sr-only" in html and ".sr-only" not in html:
        html = html.replace(
            "</style>",
            ".sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0;}\n</style>"
        )
    return html

def process_file(path: Path) -> None:
    html = path.read_text()
    original = html

    html = fix_contrast(html)
    html = add_meta_description(html)
    html = add_focus_styles(html)
    html = add_lang(html)
    html = fix_logo_link(html)
    html = fix_inputs(html)
    html = fix_select(html)
    html = add_main_landmark(html)
    html = add_aria_hidden_decorative(html)
    html = add_sr_only_class(html)

    if html != original:
        path.write_text(html)
        print(f"  fixed: {path.name}")
    else:
        print(f"  skip:  {path.name} (no changes)")

def main():
    for f in sorted(NOIR.glob("*.html")):
        if f.name == "index.html":
            continue
        process_file(f)

if __name__ == "__main__":
    main()
