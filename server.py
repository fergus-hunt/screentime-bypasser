"""
A tiny local web server for pulling downloaded media onto a client device.

Serves the media folder over the LAN with a clean browsable index. Open the
printed URL on your phone/laptop and tap a file to download it.
"""

from __future__ import annotations

import html
import socket
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import config


def _lan_ip() -> str:
    """Best-effort discovery of the Pi's LAN IP address."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually send packets; just picks the outbound interface.
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


class MediaHandler(SimpleHTTPRequestHandler):
    """Serves files from MEDIA_DIR with a friendly index page."""

    def do_DELETE(self):
        if self.path.startswith("/api/file"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            target = qs.get("target", [""])[0]
            if not target:
                self.send_error(400, "Missing target")
                return
            
            p = (config.MEDIA_DIR / target).resolve()
            if not p.is_relative_to(config.MEDIA_DIR.resolve()) or not p.exists():
                self.send_error(403, "Invalid target")
                return
            
            try:
                p.unlink()
                self.send_response(200)
                self.end_headers()
            except Exception as e:
                self.send_error(500, str(e))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/api/rename"):
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            import json
            try:
                data = json.loads(body)
                old_path = data['old']
                new_path = data['new']
                
                p_old = (config.MEDIA_DIR / old_path).resolve()
                p_new = (config.MEDIA_DIR / new_path).resolve()
                
                if not p_old.is_relative_to(config.MEDIA_DIR.resolve()) or not p_old.exists():
                    self.send_error(403, "Invalid old target")
                    return
                if not p_new.is_relative_to(config.MEDIA_DIR.resolve()):
                    self.send_error(403, "Invalid new target")
                    return
                
                p_new.parent.mkdir(parents=True, exist_ok=True)
                p_old.rename(p_new)
                self.send_response(200)
                self.end_headers()
            except Exception as e:
                self.send_error(500, str(e))
        else:
            self.send_error(404)

    def do_GET(self):
        if self.path.startswith("/api/grab"):
            self.handle_grab()
            return
        super().do_GET()

    def handle_grab(self):
        import json
        import app
        import downloader
        import reddit_archiver

        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        url = qs.get("url", [""])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        if not url:
            self.wfile.write(b"data: " + json.dumps({"status": "error", "error": "Missing URL"}).encode("utf-8") + b"\n\n")
            return

        def sse_progress(d):
            try:
                import re
                def clean(s):
                    return re.sub(r'\x1b\[[0-9;]*m', '', str(s)) if s else ""
                
                if d.get("status") == "downloading":
                    data = {
                        "status": "downloading",
                        "percent_str": clean(d.get("_percent_str", "")).strip(),
                        "speed_str": clean(d.get("_speed_str", "")).strip(),
                        "eta_str": clean(d.get("_eta_str", "")).strip(),
                    }
                    self.wfile.write(b"data: " + json.dumps(data).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                elif d.get("status") == "finished":
                    self.wfile.write(b"data: " + json.dumps({"status": "finished"}).encode("utf-8") + b"\n\n")
                    self.wfile.flush()
            except Exception:
                pass

        try:
            kind = downloader.classify(url)
            if kind == "reddit" and reddit_archiver.is_reddit_thread(url):
                self.wfile.write(b"data: " + json.dumps({"status": "archiving"}).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                
            app.cmd_download([url], audio=False, no_playlist=False, progress_hook=sse_progress)
            self.wfile.write(b"data: " + json.dumps({"status": "done"}).encode("utf-8") + b"\n\n")
            self.wfile.flush()
        except Exception as e:
            try:
                self.wfile.write(b"data: " + json.dumps({"status": "error", "error": str(e)}).encode("utf-8") + b"\n\n")
                self.wfile.flush()
            except Exception:
                pass

    def list_directory(self, path):  # noqa: D401 - override
        media_dir = config.MEDIA_DIR.resolve()
        try:
            entries = sorted(
                (p for p in media_dir.rglob('*') if p.is_file() and not p.name.startswith('.')),
                key=lambda e: str(e.relative_to(media_dir)).lower()
            )
        except OSError:
            self.send_error(404, "No permission to list directory")
            return None

        rows = []
        for e in entries:
            rel_path = str(e.relative_to(media_dir))
            display = html.escape(rel_path)              # safe to show as text
            # URL-encode the href so spaces, brackets etc. don't 404.
            href = "/" + "/".join(urllib.parse.quote(part) for part in rel_path.split("/"))
            size = _human_size(e.stat().st_size)
            # Archived Reddit threads (.html) open in the browser to read;
            # videos play natively; everything else is offered as a download.
            is_html = e.name.lower().endswith((".html", ".htm"))
            is_video = e.name.lower().endswith((".mp4", ".webm", ".mkv"))
            
            if is_html:
                link = f'<a href="{href}">📄 {display}</a>'
            elif is_video:
                link = f'<a href="{href}">🎬 {display}</a>'
            else:
                link = f'<a href="{href}" download>💾 {display}</a>'
                
            actions = (
                f'<button onclick="renameFile(\\'{html.escape(rel_path)}\\')" class="action-btn" title="Rename">✏️</button>'
                f'<button onclick="deleteFile(\\'{html.escape(rel_path)}\\')" class="action-btn delete-btn" title="Delete">🗑️</button>'
            )
            rows.append(f'<tr data-search="{html.escape(rel_path.lower())}"><td>{link}</td><td class="actions">{actions}</td><td class="size">{size}</td></tr>')

        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Media</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 760px;
          padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  .grab-form {{ display: flex; gap: 0.5rem; margin-bottom: 1rem; }}
  .search-form {{ display: flex; gap: 0.5rem; margin-bottom: 1.5rem; }}
  .search-form input[type="text"] {{ flex: 1; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; }}
  .grab-form input[type="url"] {{ flex: 1; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; }}
  .grab-form button {{ padding: 0.5rem 1rem; background: #0b6; color: white; border: none; border-radius: 4px; cursor: pointer; }}
  .grab-form button:hover {{ background: #095; }}
  #progress-container {{ display: none; margin-bottom: 1.5rem; }}
  progress {{ width: 100%; height: 20px; }}
  #progress-text {{ font-size: 0.9rem; color: #555; text-align: center; margin-top: 0.25rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: .55rem .4rem; border-bottom: 1px solid #eee; }}
  td.size {{ text-align: right; color: #777; white-space: nowrap; }}
  td.actions {{ text-align: right; white-space: nowrap; width: 80px; }}
  .action-btn {{ background: none; border: none; cursor: pointer; opacity: 0.6; font-size: 1.1rem; padding: 0 4px; }}
  .action-btn:hover {{ opacity: 1; }}
  .delete-btn:hover {{ color: red; }}
  a {{ text-decoration: none; color: #0b6; word-break: break-all; }}
  a:hover {{ text-decoration: underline; }}
  .empty {{ color: #777; }}
</style>
<script>
  function filterFiles() {{
    const q = document.getElementById("search-input").value.toLowerCase();
    document.querySelectorAll("tr[data-search]").forEach(tr => {{
      tr.style.display = tr.dataset.search.includes(q) ? "" : "none";
    }});
  }}

  async function deleteFile(path) {{
    if (!confirm(`Delete ${{path}}?`)) return;
    const res = await fetch("/api/file?target=" + encodeURIComponent(path), {{ method: "DELETE" }});
    if (res.ok) window.location.reload();
    else alert("Error deleting: " + await res.text());
  }}

  async function renameFile(oldPath) {{
    const parts = oldPath.split('/');
    const oldName = parts.pop();
    const newName = prompt("Enter new name:", oldName);
    if (!newName || newName === oldName) return;
    
    parts.push(newName);
    const newPath = parts.join('/');
    
    const res = await fetch("/api/rename", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ old: oldPath, new: newPath }})
    }});
    if (res.ok) window.location.reload();
    else alert("Error renaming: " + await res.text());
  }}

  function grabUrl(event) {{
    event.preventDefault();
    const urlInput = document.getElementById("url-input");
    const url = urlInput.value;
    if (!url) return;
    
    urlInput.disabled = true;
    document.querySelector(".grab-form button").disabled = true;
    
    const pc = document.getElementById("progress-container");
    const pbar = document.getElementById("progress-bar");
    const ptext = document.getElementById("progress-text");
    pc.style.display = "block";
    pbar.removeAttribute("value");
    ptext.innerText = "Starting download...";
    
    const es = new EventSource("/api/grab?url=" + encodeURIComponent(url));
    es.onmessage = function(e) {{
      const data = JSON.parse(e.data);
      if (data.status === "downloading") {{
        ptext.innerText = data.percent_str + " - " + data.speed_str + " - ETA: " + data.eta_str;
        let pct = parseFloat(data.percent_str);
        if (!isNaN(pct)) pbar.value = pct;
      }} else if (data.status === "finished") {{
        pbar.value = 100;
        ptext.innerText = "Done, post-processing...";
      }} else if (data.status === "done") {{
        es.close();
        window.location.reload();
      }} else if (data.status === "error") {{
        es.close();
        ptext.innerText = "Error: " + data.error;
        urlInput.disabled = false;
        document.querySelector(".grab-form button").disabled = false;
      }} else if (data.status === "archiving") {{
        ptext.innerText = "Archiving Reddit thread...";
      }}
    }};
    es.onerror = function(e) {{
      es.close();
      ptext.innerText = "Connection lost.";
      urlInput.disabled = false;
      document.querySelector(".grab-form button").disabled = false;
    }};
  }}
</script>
</head><body>
<h1>Pi Media &mdash; {len(entries)} file(s)</h1>
<div class="search-form">
  <input type="text" id="search-input" placeholder="Search files..." onkeyup="filterFiles()">
</div>
<form class="grab-form" onsubmit="grabUrl(event)">
  <input type="url" id="url-input" placeholder="Paste YouTube or Reddit link here..." required>
  <button type="submit">Grab</button>
</form>
<div id="progress-container">
  <progress id="progress-bar" max="100"></progress>
  <div id="progress-text"></div>
</div>
<table>{''.join(rows) or '<tr><td class="empty">Nothing downloaded yet.</td></tr>'}</table>
</body></html>"""

        encoded = body.encode("utf-8", "surrogateescape")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        return None

    def log_message(self, fmt, *args):  # quieter logging
        print("  [web] " + (fmt % args))


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or config.SERVER_HOST
    port = port or config.SERVER_PORT
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    handler = partial(MediaHandler, directory=str(config.MEDIA_DIR))
    httpd = ThreadingHTTPServer((host, port), handler)

    ip = _lan_ip()
    print("\nServing media folder:", config.MEDIA_DIR)
    print(f"On this device:   http://localhost:{port}/")
    print(f"From your client: http://{ip}:{port}/")
    print("Press Ctrl+C to stop.\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
