"""
Reddit thread archiver — headless-browser edition (no Reddit API/account).

Given a Reddit post URL, this loads the thread in a real headless Chromium
browser (via Playwright), expands all the comments, and saves a single
self-contained HTML file you can read offline.

Why a real browser instead of the API or a plain fetch?
  Reddit blocks unauthenticated scripted access (HTTP 403: Blocked) and the
  official API now needs an approved app + credentials. A headless browser is
  an actual browser, so Reddit serves it normally — no account, no API key, no
  captcha. We point it at old.reddit.com because that layout is light, fully
  server-rendered, and easy to expand.

Read-only by design: the saved page is a frozen local copy. The whole point is
friction — you deliberately archive a thread, then read the local copy.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import config

POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)


class ArchiverError(RuntimeError):
    """Raised with actionable guidance when archiving can't proceed."""


def is_reddit_thread(url: str) -> bool:
    """True if this looks like a Reddit *comments* page (not a bare media link)."""
    return "/comments/" in url and "reddit.com" in url


def _old_reddit_url(url: str) -> str:
    """Force any reddit post URL onto the light old.reddit layout."""
    clean = url.split("#")[0].rstrip("/")
    # Reduce to the path part, regardless of scheme/subdomain (or none at all).
    clean = re.sub(r"^https?://", "", clean)            # drop scheme if present
    clean = re.sub(r"^(www\.|new\.|np\.|old\.)?reddit\.com", "", clean)
    if not clean.startswith("/"):
        clean = "/" + clean
    return "https://old.reddit.com" + clean


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: WPS433
        return sync_playwright
    except ImportError as exc:
        raise ArchiverError(
            "Playwright is not installed. One-time setup:\n"
            "    pip install playwright\n"
            "    python -m playwright install chromium\n"
            "  (On a Pi you may also need: "
            "sudo apt install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 "
            "libcups2 libdrm2 libgbm1 libasound2)"
        ) from exc


# JavaScript run inside the page to expand every collapsed comment / "load more".
# old.reddit uses <span class="morecomments"> links and collapsed comments.
_EXPAND_JS = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let rounds = 0;
  while (rounds < 40) {
    // "load more comments" links
    const more = Array.from(document.querySelectorAll('.morecomments a'));
    // collapsed comment expanders
    const expanders = Array.from(document.querySelectorAll('.expand'))
      .filter(e => e.textContent.trim() === '[+]');
    const clickables = more.concat(expanders);
    if (clickables.length === 0) break;
    for (const el of clickables.slice(0, 25)) {
      try { el.click(); } catch (e) {}
    }
    await sleep(1200);  // let new comments load in
    rounds++;
  }
  return rounds;
}
"""


def _inline_page_html(page) -> str:
    """
    Grab the rendered HTML and lightly clean it so it stands alone offline:
    remove scripts (no live JS), and add a read-only banner.
    """
    html_content = page.content()

    # Strip <script> blocks so the saved copy is inert/offline.
    html_content = re.sub(r"<script\b[^>]*>.*?</script>", "",
                          html_content, flags=re.DOTALL | re.IGNORECASE)

    archived = datetime.now().strftime("%Y-%m-%d %H:%M")
    banner = (
        '<div style="position:sticky;top:0;z-index:9999;background:#fff8e1;'
        'border-bottom:1px solid #ffe082;padding:8px 12px;font:14px system-ui;'
        f'color:#715c00">&#128229; Offline archive &middot; saved {archived} '
        '&middot; read-only local copy.</div>'
    )
    # Insert banner right after <body>.
    if re.search(r"<body[^>]*>", html_content, re.IGNORECASE):
        html_content = re.sub(r"(<body[^>]*>)", r"\1" + banner,
                              html_content, count=1, flags=re.IGNORECASE)
    else:
        html_content = banner + html_content
    return html_content


def _slugify(text: str, maxlen: int = 80) -> str:
    slug = re.sub(r"[^\w\s-]", "", text or "").strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:maxlen] or "reddit-thread"


def archive(url: str) -> Path:
    """
    Archive a Reddit thread to a self-contained offline HTML file using a
    headless browser. Returns the path to the saved file.
    """
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    sync_playwright = _import_playwright()
    target = _old_reddit_url(url)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as exc:  # noqa: BLE001
            raise ArchiverError(
                f"Could not launch headless Chromium: {exc}\n"
                "  Make sure it's installed:  python -m playwright install chromium"
            ) from exc

        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 2000},
        )
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:  # noqa: BLE001
            browser.close()
            raise ArchiverError(
                f"Could not load the thread ({exc}).\n"
                "  Check the URL and your Pi's internet connection."
            ) from exc

        # Grab the title for the filename.
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = "reddit-thread"

        # Expand all collapsed comments / "load more" links.
        try:
            page.evaluate(_EXPAND_JS)
        except Exception:  # noqa: BLE001
            pass  # best-effort; still save whatever loaded

        html_content = _inline_page_html(page)
        browser.close()

    post_id = POST_ID_RE.search(url)
    pid = post_id.group(1) if post_id else "thread"
    title = re.sub(r"\s*:\s*r/.*$", "", title)  # trim trailing " : r/sub"
    fname = f"{_slugify(title)} [{pid}].html"
    out = config.MEDIA_DIR / fname
    out.write_text(html_content, encoding="utf-8")
    return out
