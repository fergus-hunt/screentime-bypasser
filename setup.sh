#!/usr/bin/env bash
# One-shot setup for the Pi media grabber.
# Run from this directory:  bash setup.sh
set -e

echo "==> Installing ffmpeg (needed to merge video + audio)..."
sudo apt update
sudo apt install -y ffmpeg python3-venv python3-pip

echo "==> Creating a Python virtual environment in ./venv ..."
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo
echo "Done. To use it:"
echo "  source venv/bin/activate"
echo "  python app.py 'https://youtu.be/...'        # download"
echo "  python app.py --serve                        # serve over LAN"
echo
echo "Tip: keep yt-dlp current with  pip install -U yt-dlp"
