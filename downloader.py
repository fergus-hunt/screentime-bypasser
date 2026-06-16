"""
Download logic. Wraps yt-dlp to fetch fully-formed, merged video+audio
from YouTube and Reddit links and drop them into the media folder.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import config

try:
    import yt_dlp
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "yt-dlp is not installed. Run:\n"
        "    pip install yt-dlp\n"
        "and make sure ffmpeg is installed (sudo apt install ffmpeg)."
    ) from exc


YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)
REDDIT_RE = re.compile(r"(reddit\.com|redd\.it|v\.redd\.it)", re.IGNORECASE)


def classify(url: str) -> str:
    """Return 'youtube', 'reddit', or 'other' for a given URL."""
    if YOUTUBE_RE.search(url):
        return "youtube"
    if REDDIT_RE.search(url):
        return "reddit"
    return "other"


def _check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ffmpeg not found. It is required to merge video + audio.\n"
            "Install it with:  sudo apt install ffmpeg"
        )


def _format_string() -> str:
    """
    Build a yt-dlp format selector that grabs the best video+audio and
    merges into a single file, respecting the height cap if set.
    """
    height = config.MAX_HEIGHT
    if height:
        return (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        )
    return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"


def _build_opts(progress_hook=None) -> dict:
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    postprocessors = [
        # Ensure the final container is mp4.
        {"key": "FFmpegVideoRemuxer", "preferedformat": config.CONTAINER},
    ]
    if config.EMBED_METADATA:
        postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})

    opts = {
        "format": _format_string(),
        "merge_output_format": config.CONTAINER,
        "outtmpl": str(config.MEDIA_DIR / config.OUTPUT_TEMPLATE),
        "postprocessors": postprocessors,
        "noplaylist": False,        # allow playlists, but see --no-playlist flag
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 3,
        "restrictfilenames": False,
        "windowsfilenames": False,
        "quiet": False,
        "no_warnings": False,
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts


def download(url: str, *, audio_only: bool = False, no_playlist: bool = False,
             progress_hook=None) -> list[Path]:
    """
    Download a single URL. Returns the list of resulting file paths.
    """
    _check_ffmpeg()
    opts = _build_opts(progress_hook=progress_hook)

    if no_playlist:
        opts["noplaylist"] = True

    if audio_only:
        # Override format/postprocessors for an mp3 extraction.
        opts["format"] = "bestaudio/best"
        opts["merge_output_format"] = None
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": "192"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]

    produced: list[Path] = []

    def _collect(d):
        if d.get("status") == "finished":
            fn = d.get("filename")
            if fn:
                produced.append(Path(fn))

    hooks = opts.get("progress_hooks", [])
    hooks.append(_collect)
    opts["progress_hooks"] = hooks

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Resolve the actual final filenames (post-processing may change extension).
    finals: list[Path] = []
    entries = info.get("entries") if info and "entries" in info else [info]
    for entry in entries or []:
        if not entry:
            continue
        # yt-dlp stores the requested download path; recompute final name.
        with yt_dlp.YoutubeDL(opts) as ydl:
            base = Path(ydl.prepare_filename(entry))
        ext = "mp3" if audio_only else config.CONTAINER
        final = base.with_suffix(f".{ext}")
        finals.append(final if final.exists() else base)

    # Fall back to what the progress hook captured if resolution missed.
    result = [p for p in finals if p.exists()] or [p for p in produced if p.exists()]
    return result
