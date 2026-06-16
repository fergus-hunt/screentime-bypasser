"""
Reddit thread archiver.

Given a Reddit post URL, fetch the full thread (post + all comments) from
old.reddit's JSON endpoint and render a single self-contained HTML file you
can open and read offline. Read-only by design — the whole point is to add
friction: you deliberately download a thread, then read the local copy.

Why old.reddit / the .json endpoint?
  Reddit exposes any post as JSON by appending ".json" to the permalink. That
  gives us the post and the entire comment tree as structured data — no
  JavaScript, no scraping, no fragile HTML. We render it ourselves into a clean,
  fully offline page.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import config

# A descriptive UA is polite and avoids Reddit's stricter bot throttling.
USER_AGENT = "pi-media-grabber/1.0 (personal offline archiver)"

POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)


def is_reddit_thread(url: str) -> bool:
    """True if this looks like a Reddit *comments* page (not a bare media link)."""
    return "/comments/" in url and "reddit.com" in url


def _to_json_url(url: str) -> str:
    """Normalise any reddit post URL to its old.reddit JSON form."""
    # Strip query/fragment, force old.reddit, ensure single trailing .json
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    clean = re.sub(r"https?://(www\.|new\.)?reddit\.com",
                   "https://old.reddit.com", clean)
    if not clean.startswith("http"):
        clean = "https://old.reddit.com" + clean
    return clean + "/.json"


def _fetch_json(url: str, *, retries: int = 3):
    """Fetch and parse JSON, with a couple of polite retries."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            last_err = exc
            # 429/5xx — back off and retry.
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch Reddit JSON ({url}): {last_err}")


def _fmt_time(epoch: float | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _esc(text: str | None) -> str:
    return html.escape(text or "")


def _render_comment(node: dict, depth: int = 0) -> str:
    """Recursively render a comment and its replies into nested HTML."""
    data = node.get("data", {})
    if node.get("kind") == "more":
        n = data.get("count", 0)
        return (f'<div class="more">+ {n} more repl'
                f'{"y" if n == 1 else "ies"} (not archived)</div>')
    if node.get("kind") != "t1":
        return ""

    author = _esc(data.get("author", "[deleted]"))
    score = data.get("score", 0)
    when = _fmt_time(data.get("created_utc"))
    body = data.get("body_html") or ""
    # body_html comes HTML-escaped from Reddit; unescape once so it renders.
    body = html.unescape(body)

    replies_html = ""
    replies = data.get("replies")
    if isinstance(replies, dict):
        for child in replies.get("data", {}).get("children", []):
            replies_html += _render_comment(child, depth + 1)

    return f"""
    <div class="comment" style="--depth:{depth}">
      <div class="meta"><span class="author">{author}</span>
        <span class="score">{score} pts</span>
        <span class="time">{when}</span></div>
      <div class="body">{body}</div>
      {replies_html}
    </div>"""


def _render_page(post: dict, comments: list[dict]) -> str:
    title = _esc(post.get("title"))
    author = _esc(post.get("author"))
    sub = _esc(post.get("subreddit_name_prefixed"))
    score = post.get("score", 0)
    when = _fmt_time(post.get("created_utc"))
    permalink = "https://reddit.com" + post.get("permalink", "")

    # Post body: selftext, link, image, or video reference.
    parts = []
    if post.get("selftext_html"):
        parts.append(html.unescape(post["selftext_html"]))
    url = post.get("url_overridden_by_dest") or post.get("url")
    if url and not post.get("is_self"):
        if re.search(r"\.(jpg|jpeg|png|gif|webp)$", url, re.IGNORECASE):
            parts.append(f'<img class="postimg" src="{_esc(url)}" '
                         f'alt="post image" loading="lazy">')
        else:
            parts.append(f'<p><a href="{_esc(url)}">{_esc(url)}</a></p>')
    post_body = "".join(parts) or "<p class='empty'>(text post — no body)</p>"

    comments_html = "".join(_render_comment(c) for c in comments) or \
        "<p class='empty'>No comments archived.</p>"

    archived = datetime.now().strftime("%Y-%m-%d %H:%M")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — archived</title>
<style>
  body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 820px;
          margin: 1.5rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.5; }}
  .banner {{ background: #fff8e1; border: 1px solid #ffe082; border-radius: 8px;
             padding: .5rem .8rem; font-size: .85rem; color: #715c00; margin-bottom: 1rem; }}
  h1 {{ font-size: 1.3rem; line-height: 1.3; }}
  .submeta {{ color: #777; font-size: .85rem; margin-bottom: 1rem; }}
  .submeta a {{ color: #777; }}
  .postbody {{ background: #fafafa; border: 1px solid #eee; border-radius: 8px;
               padding: .2rem 1rem; margin-bottom: 1.5rem; }}
  .postimg {{ max-width: 100%; border-radius: 6px; }}
  h2 {{ font-size: 1rem; color: #555; border-top: 1px solid #eee; padding-top: 1rem; }}
  .comment {{ margin-left: calc(var(--depth) * 16px); border-left: 2px solid #eee;
              padding: .1rem .8rem; margin-top: .6rem; }}
  .comment .meta {{ font-size: .78rem; color: #888; }}
  .comment .author {{ color: #0b6; font-weight: 600; }}
  .comment .score {{ margin-left: .5rem; }}
  .comment .time {{ margin-left: .5rem; }}
  .comment .body p {{ margin: .35rem 0; }}
  .more {{ color: #999; font-size: .8rem; font-style: italic; margin: .4rem 0 .4rem 1rem; }}
  .empty {{ color: #999; font-style: italic; }}
  img {{ max-width: 100%; }}
  a {{ color: #0b6; }}
</style></head><body>
  <div class="banner">📥 Offline archive · saved {archived} · read-only copy.
     To vote or reply, open the <a href="{_esc(permalink)}">live thread</a>.</div>
  <h1>{title}</h1>
  <div class="submeta">{sub} · posted by u/{author} · {score} pts · {when}</div>
  <div class="postbody">{post_body}</div>
  <h2>Comments</h2>
  {comments_html}
</body></html>"""


def _slugify(text: str, maxlen: int = 80) -> str:
    slug = re.sub(r"[^\w\s-]", "", text).strip().lower()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:maxlen] or "reddit-thread"


def archive(url: str) -> Path:
    """
    Download a Reddit thread to a self-contained offline HTML file.
    Returns the path to the saved file.
    """
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    json_url = _to_json_url(url)
    data = _fetch_json(json_url)

    # Reddit returns [post_listing, comments_listing].
    if not isinstance(data, list) or len(data) < 2:
        raise RuntimeError("Unexpected Reddit response shape.")

    post = data[0]["data"]["children"][0]["data"]
    comments = data[1]["data"]["children"]

    page = _render_page(post, comments)

    post_id = POST_ID_RE.search(url)
    pid = post_id.group(1) if post_id else "thread"
    fname = f"{_slugify(post.get('title', 'reddit-thread'))} [{pid}].html"
    out = config.MEDIA_DIR / fname
    out.write_text(page, encoding="utf-8")
    return out
