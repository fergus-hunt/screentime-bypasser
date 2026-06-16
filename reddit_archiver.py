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

import base64
import mimetypes
import re
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import config

POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)", re.IGNORECASE)

# Cap on how big a single embedded image may be (keeps HTML from ballooning).
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB

_IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
}

# Cache so the same image URL isn't downloaded twice within one archive run.
_img_cache: dict[str, str] = {}


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


# JavaScript run inside the page to expand EVERY collapsed comment and click
# EVERY in-page "load more comments" link, until the thread fully stabilises.
#
# Two kinds of hidden comments on old.reddit:
#   1. ".morecomments a"  -> in-page "load more comments (N replies)" links that
#      fetch and inject more comments via AJAX. We click them all, repeatedly.
#   2. ".morechildren" / "continue this thread ->" -> links to a SEPARATE page.
#      Those can't be expanded in place; we collect their URLs and the Python
#      side visits each one and merges the results (see _collect_continue_urls).
#
# The loop has no small round cap: it keeps going until a full pass finds
# nothing left to click. A generous safety ceiling prevents a pathological
# infinite loop, but it's high enough to fully load very large threads.
_EXPAND_JS = r"""
async () => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  let rounds = 0;
  const MAX_ROUNDS = 400;          // safety ceiling, effectively unlimited
  let idleAfterClicks = 0;
  while (rounds < MAX_ROUNDS) {
    // In-page "load more comments" links (NOT "continue this thread").
    const more = Array.from(document.querySelectorAll('.morecomments a'))
      .filter(a => !/continue this thread/i.test(a.textContent));
    // Collapsed comment expanders still showing [+].
    const expanders = Array.from(document.querySelectorAll('.expand'))
      .filter(e => e.textContent.trim() === '[+]');
    const clickables = more.concat(expanders);

    if (clickables.length === 0) {
      // Nothing left to click — one more idle pass to be sure, then stop.
      idleAfterClicks++;
      if (idleAfterClicks >= 2) break;
      await sleep(800);
      rounds++;
      continue;
    }
    idleAfterClicks = 0;

    // Click them all this pass (not just a slice) so nothing is left behind.
    for (const el of clickables) {
      try { el.click(); } catch (e) {}
    }
    // Wait for the AJAX-injected comments to land. Scale wait with batch size.
    await sleep(Math.min(800 + clickables.length * 40, 4000));
    rounds++;
  }
  return rounds;
}
"""


# Collect the "continue this thread ->" sub-page URLs that can't be expanded
# in place. The Python side visits each, so deeply-nested chains are captured.
_COLLECT_CONTINUE_JS = r"""
() => {
  const links = Array.from(document.querySelectorAll('.morecomments a, a'))
    .filter(a => /continue this thread/i.test(a.textContent))
    .map(a => a.href)
    .filter(Boolean);
  return Array.from(new Set(links));  // de-duplicate
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

  // Collect <img> src URLs inside a container (inline images in bodies).
  const imgsIn = (el) => el
    ? Array.from(el.querySelectorAll('img'))
        .map(i => i.src).filter(Boolean)
    : [];

  // --- the post itself ---
  const top = document.querySelector('#siteTable .thing.link')
            || document.querySelector('.thing.link');

  // Post media: image, gallery, or reddit video.
  const postMedia = { images: [], video: null, isGallery: false };
  if (top) {
    // data-url is the linked media (i.redd.it image, v.redd.it video, etc.)
    const dataUrl = top.getAttribute('data-url') || '';
    // Gallery: old.reddit renders gallery tiles with .gallery-tile img or
    // the post links to reddit.com/gallery/...
    const galleryImgs = Array.from(
      top.querySelectorAll('.media-gallery img, .gallery-tile img'))
      .map(i => i.src).filter(Boolean);
    if (galleryImgs.length) {
      postMedia.images = galleryImgs;
      postMedia.isGallery = true;
    } else if (/\.(jpg|jpeg|png|gif|webp)(\?|$)/i.test(dataUrl)
               || /(i\.redd\.it|i\.imgur\.com)/.test(dataUrl)) {
      postMedia.images = [dataUrl];
    }
    // Reddit-hosted video.
    if (/v\.redd\.it/.test(dataUrl) || top.classList.contains('has-video')) {
      postMedia.video = dataUrl || top.getAttribute('data-url') || '';
    }
    // Any preview/expando image as a fallback.
    if (!postMedia.images.length && !postMedia.video) {
      const exp = imgsIn(top.querySelector('.expando'));
      if (exp.length) postMedia.images = exp;
    }
  }

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
    media: postMedia,
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
    const images = imgsIn(bodyEl);  // inline images/gifs in the comment
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
    return { author, score, when, body, images, deleted, replies };
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
  .postimg { display: block; margin: .6rem 0; max-width: 100%; }
  video { max-width: 100%; border-radius: 8px; margin: .6rem 0; }
  .videolink { display: inline-block; margin: .4rem 0; }
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


def _fetch_image_data_uri(url: str) -> str | None:
    """
    Download an image and return a base64 data: URI so it embeds in the HTML
    and works fully offline. Returns None if it can't be fetched.
    """
    if not url or url.startswith("data:"):
        return url or None
    if url in _img_cache:
        return _img_cache[url]
    try:
        req = urllib.request.Request(url, headers=_IMG_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
            data = resp.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None
    if len(data) > MAX_IMAGE_BYTES:
        return None  # too big to embed
    if not ctype or not ctype.startswith("image"):
        guessed, _ = mimetypes.guess_type(url)
        ctype = guessed or "image/jpeg"
    b64 = base64.b64encode(data).decode("ascii")
    uri = f"data:{ctype};base64,{b64}"
    _img_cache[url] = uri
    return uri


def _embed_images_in_html(body_html: str) -> str:
    """Replace every <img src> in a body fragment with an embedded data URI."""
    def _replace(match: re.Match) -> str:
        src = match.group(1)
        uri = _fetch_image_data_uri(src)
        if uri:
            return f'src="{uri}"'
        return match.group(0)  # leave original if fetch failed
    return re.sub(r'src="([^"]+)"', _replace, body_html or "")


def _media_block(media: dict) -> str:
    """Render the post's primary media (images/gallery/video) as embedded HTML."""
    if not media:
        return ""
    parts = []
    video = media.get("video")
    if video:
        # Video is downloaded separately (see archive()); placeholder filled later.
        parts.append(f'<!--VIDEO:{_esc(video)}-->')
    for img_url in media.get("images", []):
        uri = _fetch_image_data_uri(img_url)
        if uri:
            parts.append(f'<img class="postimg" src="{uri}" '
                         f'alt="post image" loading="lazy">')
    return "".join(parts)


def _embed_comment_images(comments: list) -> None:
    """In-place: embed inline images in every comment body."""
    for c in comments:
        if c.get("body"):
            c["body"] = _embed_images_in_html(c["body"])
        if c.get("replies"):
            _embed_comment_images(c["replies"])


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

    # Post body: selftext (with embedded inline images), the post's media
    # (images/gallery/video), and any external link.
    body_parts = []
    if post.get("selftext"):
        body_parts.append(_embed_images_in_html(post["selftext"]))
    body_parts.append(_media_block(post.get("media") or {}))
    link = post.get("link") or ""
    has_media = bool((post.get("media") or {}).get("images")
                     or (post.get("media") or {}).get("video"))
    if link and not post.get("selftext") and not has_media \
            and "reddit.com" not in link:
        body_parts.append(f'<p><a href="{_esc(link)}">{_esc(link)}</a></p>')
    post_body = "".join(p for p in body_parts if p) \
        or '<p class="empty">(text post — no body)</p>'

    # Embed inline images in comment bodies so they work offline.
    _embed_comment_images(comments)
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


def _json_url(url: str) -> str:
    """Reddit JSON endpoint for a thread, asking for the full comment tree."""
    base = _old_reddit_url(url)
    # raw_json=1 -> don't HTML-escape; limit=500 -> grab as many as possible.
    return base + "/.json?raw_json=1&limit=500"


def _browser_get_json(page, url: str):
    """
    Fetch a URL's JSON *through the browser context* (so it carries real browser
    headers/cookies and isn't bot-blocked like a plain script request). Returns
    the parsed object, or None on failure.
    """
    import json
    target = _json_url(url)
    try:
        resp = page.request.get(target, timeout=45000)
        if not resp.ok:
            return None
        return json.loads(resp.text())
    except Exception:  # noqa: BLE001
        return None


def _parse_listing_json(data: dict, post_url: str) -> dict:
    """
    Turn Reddit's [post_listing, comments_listing] JSON into our internal
    {post, comments} structure. Robust to layout/consent-wall changes because
    it reads structured data, not the rendered DOM.
    """
    if not isinstance(data, list) or len(data) < 2:
        return {}

    try:
        pdata = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        return {}

    # Post media.
    media = {"images": [], "video": None, "isGallery": False}
    url_dest = pdata.get("url_overridden_by_dest") or pdata.get("url") or ""
    if pdata.get("is_gallery") and pdata.get("media_metadata"):
        for item in pdata["media_metadata"].values():
            src = (item.get("s") or {}).get("u") or (item.get("s") or {}).get("gif")
            if src:
                media["images"].append(src.replace("&amp;", "&"))
        media["isGallery"] = True
    elif re.search(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", url_dest, re.IGNORECASE) \
            or re.search(r"(i\.redd\.it|i\.imgur\.com)", url_dest):
        media["images"] = [url_dest]
    if pdata.get("is_video") or "v.redd.it" in url_dest:
        media["video"] = url_dest or "v.redd.it"

    post = {
        "title": pdata.get("title") or "",
        "author": pdata.get("author") or "[deleted]",
        "subreddit": pdata.get("subreddit_name_prefixed")
                     or ("r/" + (pdata.get("subreddit") or "")),
        "score": str(pdata.get("score", "")),
        "when": _epoch_iso(pdata.get("created_utc")),
        "selftext": pdata.get("selftext_html") or "",
        "link": url_dest or post_url,
        "permalink": "https://www.reddit.com" + (pdata.get("permalink") or ""),
        "media": media,
    }
    # selftext_html is HTML-entity-escaped once; unescape so it renders.
    if post["selftext"]:
        import html as _h
        post["selftext"] = _h.unescape(post["selftext"])

    comments = _parse_comment_children(
        data[1].get("data", {}).get("children", []))
    return {"post": post, "comments": comments,
            "_more": _collect_more_ids(data[1].get("data", {}).get("children", []))}


def _epoch_iso(epoch) -> str:
    if not epoch:
        return ""
    try:
        from datetime import timezone
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return ""


def _parse_comment_children(children: list) -> list:
    """Recursively parse a JSON comment listing into our comment dicts."""
    out = []
    for child in children or []:
        if child.get("kind") != "t1":
            continue  # skip 'more' stubs here; handled separately
        d = child.get("data", {})
        body_html = d.get("body_html") or ""
        if body_html:
            import html as _h
            body_html = _h.unescape(body_html)
        replies = []
        rep = d.get("replies")
        if isinstance(rep, dict):
            replies = _parse_comment_children(
                rep.get("data", {}).get("children", []))
        out.append({
            "author": d.get("author") or "[deleted]",
            "score": str(d.get("score", "")) if d.get("score") is not None else "",
            "when": _epoch_iso(d.get("created_utc")),
            "body": body_html,
            "images": [],
            "deleted": d.get("author") in (None, "[deleted]"),
            "replies": replies,
        })
    return out


def _collect_more_ids(children: list) -> list:
    """Gather IDs of 'more comments' stubs so we can fetch them too."""
    more = []
    for child in children or []:
        if child.get("kind") == "more":
            d = child.get("data", {})
            more.extend(d.get("children", []))
        elif child.get("kind") == "t1":
            rep = child.get("data", {}).get("replies")
            if isinstance(rep, dict):
                more.extend(_collect_more_ids(
                    rep.get("data", {}).get("children", [])))
    return more


def _merge_more_comments(page, post_url: str, data: dict, more_ids: list) -> None:
    """
    Fetch 'more comments' stubs in batches via Reddit's JSON and append the
    extra top-level comments. Best-effort and capped so huge threads stay sane.
    """
    import json
    link_id = data["post"].get("permalink", "")
    m = POST_ID_RE.search(link_id) or POST_ID_RE.search(post_url)
    if not m:
        return
    article = m.group(1)
    ids = list(dict.fromkeys(more_ids))  # de-dupe, keep order
    batches = 0
    while ids and batches < 50:  # cap total batches
        chunk, ids = ids[:100], ids[100:]
        api = (f"https://www.reddit.com/api/morechildren.json?api_type=json"
               f"&link_id=t3_{article}&children={','.join(chunk)}&raw_json=1")
        try:
            resp = page.request.get(api, timeout=45000)
            if not resp.ok:
                break
            payload = json.loads(resp.text())
            things = payload.get("json", {}).get("data", {}).get("things", [])
        except Exception:  # noqa: BLE001
            break
        new_comments = _parse_comment_children(
            [t for t in things if t.get("kind") == "t1"])
        if new_comments:
            data["comments"].extend(new_comments)
        # queue any further 'more' stubs these returned
        ids.extend(_collect_more_ids(things))
        batches += 1


def _load_expand_extract(page, target: str, *, is_main: bool):
    """
    Navigate to a thread (or 'continue this thread' sub-page), fully expand all
    in-page comments, and extract structured data. Returns (data, continue_urls)
    where continue_urls are sub-pages still to visit.

    DOM-based fallback path (used only if JSON extraction fails).
    """
    try:
        page.goto(target, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:  # noqa: BLE001
        if is_main:
            raise ArchiverError(
                f"Could not load the thread ({exc}).\n"
                "  Check the URL and your Pi's internet connection."
            ) from exc
        raise ArchiverError(str(exc)) from exc

    # Expand every collapsed comment / in-page "load more" link until stable.
    try:
        page.evaluate(_EXPAND_JS)
    except Exception:  # noqa: BLE001
        pass  # best-effort; extract whatever loaded

    try:
        data = page.evaluate(_EXTRACT_JS)
    except Exception as exc:  # noqa: BLE001
        if is_main:
            raise ArchiverError(
                f"Loaded the page but couldn't extract the thread ({exc}).\n"
                "  Reddit may have changed its layout — let me know and I'll fix it."
            ) from exc
        return {}, []

    try:
        continue_urls = page.evaluate(_COLLECT_CONTINUE_JS) or []
    except Exception:  # noqa: BLE001
        continue_urls = []

    return data, continue_urls


def archive(url: str) -> Path:
    """
    Archive a Reddit thread to a self-contained offline HTML file using a
    headless browser. Returns the path to the saved file.

    Captures *every* comment: it expands all in-page "load more" links until the
    page stabilises, then follows each "continue this thread ->" sub-page and
    merges those comments in too.
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

        # --- Primary path: fetch the thread JSON through the browser. ---
        # This reads structured data, so it's immune to consent walls / layout
        # changes that break DOM scraping (the cause of blank archives).
        data = {}
        raw = _browser_get_json(page, url)
        if raw:
            data = _parse_listing_json(raw, url)

        # --- Fallback: DOM scraping (old behaviour) if JSON gave nothing. ---
        if not data or not data.get("post") or not data["post"].get("title"):
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=45000)
                page.evaluate(_EXPAND_JS)
                dom_data = page.evaluate(_EXTRACT_JS)
                if dom_data and dom_data.get("post", {}).get("title"):
                    data = dom_data
            except Exception:  # noqa: BLE001
                pass

        # Fetch any "more comments" stubs the first JSON page didn't inline, so
        # we capture the complete tree even on large threads.
        more_ids = (data or {}).get("_more", []) if isinstance(data, dict) else []
        if data and data.get("post") and more_ids:
            _merge_more_comments(page, url, data, more_ids)

        browser.close()

    if not data or not data.get("post") or not data["post"].get("title"):
        raise ArchiverError(
            "Couldn't read that thread. Either it's not a direct post link "
            "(needs a /comments/ URL), the post was removed, or Reddit blocked "
            "the request. Try again in a minute, or send me the link."
        )

    html_content = _render_page(data)

    # If the post is a Reddit-hosted video, download it (merged with audio) via
    # the existing downloader and swap the placeholder for an inline player.
    html_content = _attach_video(html_content, url)

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


def _attach_video(html_content: str, post_url: str) -> str:
    """
    Replace the <!--VIDEO:...--> placeholder with a real <video> player by
    downloading the Reddit video (with audio) via yt-dlp. Falls back to a link
    if the download isn't possible.
    """
    m = re.search(r"<!--VIDEO:(.*?)-->", html_content)
    if not m:
        return html_content

    try:
        import downloader  # local module; uses yt-dlp + ffmpeg
        # Download the *post* URL (yt-dlp resolves the v.redd.it video+audio).
        files = downloader.download(post_url, no_playlist=True)
    except Exception:  # noqa: BLE001
        files = []

    video_file = next((f for f in files
                       if f.suffix.lower() in (".mp4", ".webm", ".mkv")), None)
    if video_file and video_file.exists():
        # The HTML lives in the same media folder, so a relative src works both
        # on disk and when served by the local web server.
        import urllib.parse
        src = urllib.parse.quote(video_file.name)
        player = (f'<video controls preload="metadata" src="{src}"></video>'
                  f'<div><a class="videolink" href="{src}" download>'
                  f'&#11015; download video ({video_file.name})</a></div>')
    else:
        live = _esc(m.group(1)) or _esc(post_url)
        player = (f'<p class="empty">[video could not be downloaded — '
                  f'<a href="{live}">view on Reddit</a>]</p>')

    return html_content.replace(m.group(0), player)
