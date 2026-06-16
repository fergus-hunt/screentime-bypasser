"""
A tiny local web server for pulling downloaded media onto a client device.

Serves the media folder over the LAN with a clean browsable index. Open the
printed URL on your phone/laptop and tap a file to download it.
"""

from __future__ import annotations

import html
import socket
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

    def list_directory(self, path):  # noqa: D401 - override
        try:
            entries = sorted(
                (p for p in __import__("os").scandir(path) if p.is_file()),
                key=lambda e: e.name.lower(),
            )
        except OSError:
            self.send_error(404, "No permission to list directory")
            return None

        rows = []
        for e in entries:
            name = html.escape(e.name)
            size = _human_size(e.stat().st_size)
            # Archived Reddit threads (.html) open in the browser to read;
            # everything else is offered as a download.
            if e.name.lower().endswith((".html", ".htm")):
                link = f'<a href="{name}">📄 {name}</a>'
            else:
                link = f'<a href="{name}" download>{name}</a>'
            rows.append(f'<tr><td>{link}</td><td class="size">{size}</td></tr>')

        body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Media</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 760px;
          padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  table {{ width: 100%; border-collapse: collapse; }}
  td {{ padding: .55rem .4rem; border-bottom: 1px solid #eee; }}
  td.size {{ text-align: right; color: #777; white-space: nowrap; }}
  a {{ text-decoration: none; color: #0b6; word-break: break-all; }}
  a:hover {{ text-decoration: underline; }}
  .empty {{ color: #777; }}
</style></head><body>
<h1>Pi Media &mdash; {len(entries)} file(s)</h1>
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
