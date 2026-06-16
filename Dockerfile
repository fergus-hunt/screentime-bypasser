# Pi Media Grabber — container image.
#
# Based on the official Playwright Python image, which already bundles Chromium
# and every system library it needs (and supports arm64, so it runs on a
# Raspberry Pi 4/5 running 64-bit Raspberry Pi OS).
#
# The container runs the always-on LAN web server by default. You add downloads
# by exec'ing into the running container (see ./grab and docker-compose.yml).

FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# ffmpeg is needed to merge video+audio for downloads. The Playwright image is
# Debian/Ubuntu based, so apt works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium is already in the base image; ensure the browser Playwright expects
# is present (no-op if already installed, cheap insurance across versions).
RUN python -m playwright install chromium

# App code.
COPY . .

# Media lives on a mounted volume (see docker-compose.yml). Default inside the
# container; the volume maps it to ./media on the host.
ENV MEDIA_DIR=/data/media
ENV SERVER_PORT=8000
EXPOSE 8000

# Default command: run the always-on web server. Downloads run via `docker exec`.
CMD ["python", "app.py", "--serve"]
