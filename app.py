#!/usr/bin/env python3
"""
Pi Media Grabber — CLI

Download a YouTube or Reddit link as fully-formed media on your Raspberry Pi,
then serve the media folder over your LAN so a client device can pull it.

Usage:
    python app.py <link> [<link> ...]        # download one or more links
    python app.py <link> --audio             # extract mp3 instead of video
    python app.py <link> --no-playlist       # ignore playlists, grab single item
    python app.py --serve                    # start the local web server
    python app.py <link> --serve             # download, then serve
    python app.py --list                     # list what's already downloaded

Run `python app.py -h` for the full option list.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import config
import downloader
import reddit_archiver
import server


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}PB"


def _progress(d):
    if d.get("status") == "downloading":
        pct = d.get("_percent_str", "").strip()
        speed = d.get("_speed_str", "").strip()
        eta = d.get("_eta_str", "").strip()
        sys.stdout.write(f"\r  {pct:>6}  {speed:>10}  ETA {eta:<6}")
        sys.stdout.flush()
    elif d.get("status") == "finished":
        sys.stdout.write("\r  done, post-processing...                  \n")
        sys.stdout.flush()


def cmd_download(links: list[str], audio: bool, no_playlist: bool,
                 force_media: bool = False, progress_hook=None) -> list[Path]:
    all_files: list[Path] = []
    for url in links:
        kind = downloader.classify(url)

        # Reddit *comment threads* are archived as an offline, read-only HTML
        # page (post + full comment tree). This is the friction-by-design path:
        # you deliberately download a thread, then read the local copy.
        # Pass --media to instead pull video from a Reddit video post.
        if kind == "reddit" and reddit_archiver.is_reddit_thread(url) \
                and not force_media and not audio:
            print(f"\n[reddit:archive] {url}")
            try:
                out = reddit_archiver.archive(url)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR: {exc}")
                continue
            size = _human_size(out.stat().st_size) if out.exists() else "?"
            print(f"  saved offline page: {out.name}  ({size})")
            all_files.append(out)
            continue

        if kind == "other":
            print(f"! Not a recognised YouTube/Reddit link, trying anyway: {url}")
        else:
            print(f"\n[{kind}] {url}")
        try:
            files = downloader.download(
                url, audio_only=audio, no_playlist=no_playlist,
                progress_hook=progress_hook or _progress,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR: {exc}")
            continue
        for f in files:
            size = _human_size(f.stat().st_size) if f.exists() else "?"
            print(f"  saved: {f.name}  ({size})")
        all_files.extend(files)
    return all_files


def cmd_list() -> None:
    media = config.MEDIA_DIR
    if not media.exists():
        print(f"No media folder yet ({media}).")
        return
    files = sorted(p for p in media.iterdir() if p.is_file())
    if not files:
        print(f"Media folder is empty ({media}).")
        return
    print(f"Media in {media}:")
    total = 0
    for f in files:
        size = f.stat().st_size
        total += size
        print(f"  {_human_size(size):>9}  {f.name}")
    print(f"  {'-' * 9}")
    print(f"  {_human_size(total):>9}  total ({len(files)} files)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app.py",
        description="Download YouTube/Reddit media on a Pi and serve it over LAN.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("links", nargs="*", help="One or more YouTube/Reddit URLs.")
    p.add_argument("--audio", action="store_true",
                   help="Extract audio as MP3 instead of downloading video.")
    p.add_argument("--no-playlist", action="store_true",
                   help="If a link points into a playlist, grab only that item.")
    p.add_argument("--media", action="store_true",
                   help="For a Reddit thread, download its video/media instead "
                        "of archiving the thread as an offline page.")
    p.add_argument("--serve", action="store_true",
                   help="Start the local web server (after any downloads).")
    p.add_argument("--list", action="store_true",
                   help="List already-downloaded media and exit.")
    p.add_argument("--port", type=int, default=config.SERVER_PORT,
                   help=f"Web server port (default {config.SERVER_PORT}).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list:
        cmd_list()
        return 0

    if not args.links and not args.serve:
        build_parser().print_help()
        return 1

    if args.links:
        files = cmd_download(args.links, args.audio, args.no_playlist,
                             force_media=args.media)
        print(f"\nFinished. {len(files)} file(s) in {config.MEDIA_DIR}")

    if args.serve:
        server.serve(port=args.port)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
