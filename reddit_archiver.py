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


# JavaScript run inside the page to *extract* the thread as structured data.
# We read old.reddit's DOM (stable class names) and return clean JSON, then
# render our own readable page from it — far more robust than saving Reddit's
# raw markup, whose external CSS won't load offline anyway.
_EXTRACT_JS = r"""
() => {
  const txt = (el) => el ? el.textContent.trim() : '';
  const htmlOf = (el) => el ? el.innerHTML : '';

  // --- the post itself ---
  const top = document.querySelector('#siteTable .thing.link')
            || document.querySelector('.thing.link');
  const post = {
    title: txt(document.querySelector('a.title')) || document.title,
    author: txt(document.querySelector('.thing.link .author')),
    subreddit: txt(document.querySelector('.subreddit')) ||
               (location.pathname.match(/\/r\/([^/]+)/) || [])[1],
    score: txt(top && top.querySelector('.score.unvoted')),
    when: (top && top.querySelector('time')
            && top.querySelector('time').getAttribute('datetime')) || '',
    selftext: htmlOf(document.querySelector('.expando .usertext-body .md')) ||
              htmlOf(document.querySelector('.thing.link .usertext-body .md')),
    link: (document.querySelector('a.title') || {}).href || location.href,
    permalink: location.href,
  };

  // --- recursively walk the comment tree ---
  function parseComment(node) {
    // node is a .thing.comment
    const entry = node.querySelector(':scope > .entry');
    if (!entry) return null;
    const author = txt(entry.querySelector('.author'));
    const score = txt(entry.querySelector('.score.unvoted'))
               || txt(entry.querySelector('.score.likes'))
               || txt(entry.querySelector('.score.dislikes'));
    const timeEl = entry.querySelector('time');
    const when = timeEl ? timeEl.getAttribute('datetime') : '';
    const bodyEl = entry.querySelector('.usertext-body .md');
    const body = bodyEl ? bodyEl.innerHTML : '';
    const deleted = node.classList.contains('deleted')
                 || node.classList.contains('collapsed');

    // child comments live in :scope > .child > .sitetable > .thing.comment
    const replies = [];
    const childList = node.querySelector(':scope > .child > .sitetable');
    if (childList) {
      for (const c of childList.children) {
        if (c.classList && c.classList.contains('comment')) {
          const parsed = parseComment(c);
          if (parsed) replies.push(parsed);
        }
      }
    }
    return { author, score, when, body, deleted, replies };
  }

  const comments = [];
  const root = document.querySelector('.commentarea > .sitetable');
  if (root) {
    for (const c of root.children) {
      if (c.classList && c.classList.contains('comment')) {
        const parsed = parseComment(c);
        if (parsed) comments.push(parsed);
      }
    }
  }
  return { post, comments };
}
"""


def _esc(text) -> str:
    import html as _h
    return _h.escape(str(text or ""))


def _fmt_iso(iso: str) -> str:
    if not iso:
        return ""
    try:
        # old.reddit datetime attr looks like "2022-09-13T20:11:45+00:00"
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso


def _render_comment(c: dict, depth: int = 0) -> str:
    author = _esc(c.get("author") or "[deleted]")
    score = _esc(c.get("score"))
    when = _esc(_fmt_iso(c.get("when", "")))
    body = c.get("body") or ""  # already HTML from Reddit's rendered markdown
    replies = "".join(_render_comment(r, depth + 1) for r in c.get("replies", []))
    score_html = f'<span class="score">{score}</span>' if score else ""
    return f"""
    <div class="comment" style="--depth:{min(depth, 12)}">
      <div class="cmeta"><span class="author">{author}</span>
        {score_html}<span class="when">{when}</span></div>
      <div class="cbody">{body}</div>
      {replies}
    </div>"""


_STYLE = """
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 820px; margin: 0 auto; padding: 0 1rem 3rem;
         line-height: 1.5; color: #1a1a1a; background: #fff; }
  .banner { position: sticky; top: 0; z-index: 9; background: #fff8e1;
            border-bottom: 1px solid #ffe082; padding: .5rem .8rem;
            font-size: .85rem; color: #715c00; margin: 0 -1rem 1.2rem; }
  .banner a { color: #715c00; }
  h1 { font-size: 1.35rem; line-height: 1.3; margin: 1.2rem 0 .3rem; }
  .submeta { color: #777; font-size: .85rem; margin-bottom: 1rem; }
  .postbody { background: #fafafa; border: 1px solid #eee; border-radius: 8px;
              padding: .2rem 1rem; margin-bottom: 1.4rem; }
  .postbody img, .cbody img { max-width: 100%; height: auto; border-radius: 6px; }
  h2.section { font-size: 1rem; color: #555; border-top: 1px solid #eee;
               padding-top: 1rem; }
  .comment { margin-left: calc(var(--depth) * 14px); border-left: 2px solid #e6e6e6;
             padding: .15rem .8rem; margin-top: .55rem; }
  .cmeta { font-size: .78rem; color: #888; }
  .cmeta .author { color: #0a7d3e; font-weight: 600; }
  .cmeta .score { margin-left: .5rem; }
  .cmeta .when { margin-left: .5rem; }
  .cbody p { margin: .35rem 0; }
  .cbody pre { background: #f4f4f4; padding: .6rem .8rem; border-radius: 6px;
               overflow-x: auto; font-size: .85rem; }
  .cbody code { background: #f4f4f4; padding: .1rem .3rem; border-radius: 4px; }
  .cbody blockquote { border-left: 3px solid #ddd; margin: .4rem 0;
                      padding-left: .8rem; color: #555; }
  a { color: #0a7d3e; }
  .empty { color: #999; font-style: italic; }
  @media (prefers-color-scheme: dark) {
    body { background: #16181c; color: #d7dadc; }
    .postbody { background: #1f2226; border-color: #2a2d31; }
    .cmeta .author { color: #4ac776; }
    a { color: #4ac776; }
    .cbody pre, .cbody code { background: #2a2d31; }
  }
"""


def _count_comments(comments: list) -> int:
    return sum(1 + _count_comments(c.get("replies", [])) for c in comments)


def _render_page(data: dict) -> str:
    post = data.get("post", {})
    comments = data.get("comments", [])

    title = _esc(post.get("title") or "Reddit thread")
    author = _esc(post.get("author") or "[deleted]")
    sub = _esc(post.get("subreddit") or "")
    if sub and not sub.startswith("r/"):
        sub = "r/" + sub
    score = _esc(post.get("score"))
    when = _esc(_fmt_iso(post.get("when", "")))
    permalink = _esc(post.get("permalink") or post.get("link") or "")

    # Post body: selftext if present, else the external link / image.
    body_parts = []
    if post.get("selftext"):
        body_parts.append(post["selftext"])
    link = post.get("link") or ""
    if link and not post.get("selftext"):
        if re.search(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", link, re.IGNORECASE):
            body_parts.append(f'<img src="{_esc(link)}" alt="post image" loading="lazy">')
        elif "reddit.com" not in link:
            body_parts.append(f'<p><a href="{_esc(link)}">{_esc(link)}</a></p>')
    post_body = "".join(body_parts) or '<p class="empty">(text post — no body)</p>'

    comments_html = "".join(_render_comment(c) for c in comments) \
        or '<p class="empty">No comments archived.</p>'
    n = _count_comments(comments)
    archived = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — archived</title>
<style>{_STYLE}</style></head><body>
  <div class="banner">&#128229; Offline archive · saved {archived} · read-only.
     <a href="{permalink}">live thread</a></div>
  <h1>{title}</h1>
  <div class="submeta">{sub} · posted by u/{author}
     {(' · ' + score + ' pts') if score else ''}{(' · ' + when) if when else ''}</div>
  <div class="postbody">{post_body}</div>
  <h2 class="section">{n} comment{'s' if n != 1 else ''}</h2>
  {comments_html}
</body></html>"""


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

        # Expand all collapsed comments / "load more" links first.
        try:
            page.evaluate(_EXPAND_JS)
        except Exception:  # noqa: BLE001
            pass  # best-effort; extract whatever loaded

        # Extract the thread as structured data, then render our own clean page.
        try:
            data = page.evaluate(_EXTRACT_JS)
        except Exception as exc:  # noqa: BLE001
            browser.close()
            raise ArchiverError(
                f"Loaded the page but couldn't extract the thread ({exc}).\n"
                "  Reddit may have changed its layout — let me know and I'll update."
            ) from exc
        browser.close()

    if not data or not data.get("post"):
        raise ArchiverError(
            "No thread content found at that URL. Make sure it's a link to a "
            "specific Reddit post (a /comments/ URL)."
        )

    html_content = _render_page(data)
    title = data["post"].get("title") or "reddit-thread"
    post_id = POST_ID_RE.search(url)
    pid = post_id.group(1) if post_id else "thread"
    title = re.sub(r"\s*:\s*r/.*$", "", title)  # trim trailing " : r/sub"
    # No brackets/spaces in the filename — they break download URLs (the [id]
    # was causing 404s when the file was fetched through the web server).
    fname = f"reddit-{_slugify(title, maxlen=70)}-{pid}.html"
    out = config.MEDIA_DIR / fname
    out.write_text(html_content, encoding="utf-8")
    return out
