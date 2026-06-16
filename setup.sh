#!/usr/bin/env bash
# One-shot setup for the Pi media grabber.
# Run from this directory:  bash setup.sh
set -e

echo "==> Installing system packages (ffmpeg + headless-browser libs)..."
sudo apt update
# ffmpeg: merge video+audio. The rest: shared libs Chromium needs on a Pi.
sudo apt install -y ffmpeg python3-venv python3-pip \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libgbm1 libasound2 libpangocairo-1.0-0 libxkbcommon0 libxdamage1

echo "==> Creating a Python virtual environment in ./venv ..."
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate

echo "==> Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing headless Chromium for Reddit archiving (this can take a few minutes)..."
python -m playwright install chromium

echo
echo "Done. To use it:"
echo "  source venv/bin/activate"
echo "  python app.py 'https://youtu.be/...'                 # download video"
echo "  python app.py 'https://reddit.com/r/.../comments/...' # archive a thread"
echo "  python app.py --serve                                 # serve over LAN"
echo
echo "Tip: keep yt-dlp current with  pip install -U yt-dlp"
