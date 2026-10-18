"""
Central configuration for the Pi media grabber.

Edit these values to suit your setup. Everything else reads from here.
"""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Where downloaded media is stored. Defaults to 'media' in the working directory.
# Override with the MEDIA_DIR environment variable if you like.
# ---------------------------------------------------------------------------
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", Path.cwd() / "media")).resolve()

# ---------------------------------------------------------------------------
# Local web server (for pulling files onto your client device over the LAN).
# ---------------------------------------------------------------------------
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")  # 0.0.0.0 = all interfaces
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8000"))

# ---------------------------------------------------------------------------
# Download behaviour.
# ---------------------------------------------------------------------------
# Target container. mp4 is the most universally playable.
CONTAINER = "mp4"

# Max video height to grab (saves bandwidth/space on a Pi). None = best available.
MAX_HEIGHT = int(os.environ["MAX_HEIGHT"]) if os.environ.get("MAX_HEIGHT") else 1080

# Embed thumbnail + metadata into the file where possible.
EMBED_METADATA = True

# Filename template (yt-dlp output template syntax).
# Keeps things tidy: "Title [video-id].mp4"
OUTPUT_TEMPLATE = "%(title).200B [%(id)s].%(ext)s"
