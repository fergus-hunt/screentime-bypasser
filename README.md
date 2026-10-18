# Pi Media Grabber

A small command-line app for a Raspberry Pi. Give it a YouTube or Reddit link;
it downloads fully-formed, merged video+audio media locally, then serves your
media folder over the LAN so any client device can pull the files from a browser.

## What it does

- Detects YouTube vs Reddit links automatically.
- Downloads the best available video **and** audio and merges them into a single
  `.mp4` (via `yt-dlp` + `ffmpeg`), capped at 1080p by default to be kind to the Pi.
- Embeds title/metadata into the file.
- Runs a lightweight web server so your phone or laptop can browse the media
  folder and download files with a tap — no client-side setup, no SSH keys.

## Run it always-on with Docker (recommended)

This is the easiest way to keep the server running 24/7 on your Pi. Docker
bundles Python, ffmpeg and Chromium into one image, so there's nothing to
install on the Pi except Docker itself.

One-time, on the Pi:

```bash
# Install Docker (skip if you already have it)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # then log out/in so 'docker' works without sudo

cd restriction-bypasser
docker compose up -d --build       # build image + start the always-on server
```

That's it — the LAN server is now running and will **restart on boot and after
crashes** (`restart: unless-stopped`). Find it from any device on your network at
`http://<your-pi-ip>:8000/`.

To **download something**, use the included helper (it runs inside the running
container):

```bash
chmod +x grab          # one time
./grab "https://youtu.be/..."                      # download a video
./grab "https://www.reddit.com/r/.../comments/..."  # archive a Reddit thread
./grab "https://youtu.be/..." --audio               # flags pass through to the app
```

Anything you grab appears immediately at `http://<your-pi-ip>:8000/`.
Downloaded files live in `./media` on the Pi (a Docker volume), so they persist
across rebuilds and you can browse them directly too.

Everyday Docker commands:

```bash
docker compose ps             # is it running / healthy?
docker compose logs -f        # watch server logs
docker compose restart        # restart it
docker compose down           # stop it (and disable always-on until next 'up')
docker compose up -d --build  # rebuild after you change the code
```

## Install without Docker (on the Pi)

If you'd rather not use Docker:

```bash
cd restriction-bypasser
bash setup.sh
```

This installs `ffmpeg`, `yt-dlp` and headless Chromium into a local `./venv`.
Then activate it whenever you use the app:

```bash
source venv/bin/activate
```

## Reddit threads: offline, read-only archive

When you give it a **Reddit comments link**, the app does *not* open Reddit — it
downloads the whole thread (post + full comment tree) into a single, self-contained
HTML file you read offline. This is deliberate friction: to read a thread you have
to consciously archive it first, then open the local copy. No live feed, no
infinite scroll, no voting — just the one thread you decided was worth it.

### How it works (no Reddit account or API key needed)

Reddit blocks plain scripted access (`HTTP 403: Blocked`) and its official API
now needs an approved app. So instead, the archiver uses a **real headless
browser** (Chromium via Playwright): it loads the thread the way a human would,
expands all the comments, and saves the page. No login, no API key, no captcha.

The only setup is installing Chromium once — `setup.sh` does this for you, or
manually:

```bash
pip install playwright
python -m playwright install chromium
```

On a Raspberry Pi the first archive may take a little longer while the browser
starts up; subsequent ones are quicker.

```bash
# Archive a Reddit thread for offline reading
python app.py "https://www.reddit.com/r/node/comments/xcmqpd/..."

# Then serve it and open it from any device (it opens in-browser to read)
python app.py --serve
```

If a Reddit link is actually a *video* post and you want the video file instead
of the thread, add `--media`:

```bash
python app.py "https://www.reddit.com/r/aww/comments/.../" --media
```

## Usage

```bash
# Download a single link (video + audio, merged mp4)
python app.py "https://youtu.be/dQw4w9WgXcQ"

# Download several at once
python app.py "https://youtube.com/watch?v=..." "https://reddit.com/r/.../comments/..."

# Audio only (mp3)
python app.py "https://youtu.be/..." --audio

# A link that sits inside a playlist — grab just that one item
python app.py "https://youtube.com/watch?v=...&list=..." --no-playlist

# Download, then immediately start serving over the LAN
python app.py "https://youtu.be/..." --serve

# Just start the server (serve whatever is already downloaded)
python app.py --serve

# See what's downloaded
python app.py --list
```

### Pulling files onto your client device

When the server starts it prints two URLs, e.g.:

```
On this device:   http://localhost:8000/
From your client: http://192.168.1.42:8000/
```

Open the **client** URL on your phone or laptop (same Wi-Fi/LAN), and tap any
file to download it.

## Configuration

Edit `config.py`, or set environment variables:

| Variable      | Default        | Meaning                                  |
|---------------|----------------|------------------------------------------|
| `MEDIA_DIR`   | `~/media`      | Where downloads are stored.              |
| `SERVER_PORT` | `8000`         | LAN web server port.                     |
| `MAX_HEIGHT`  | `1080`         | Max video height (set empty for best).   |

Example:

```bash
MEDIA_DIR=/mnt/usb/media MAX_HEIGHT=720 python app.py "https://youtu.be/..."
```

## Run the server on boot (optional)

See `pi-media-server.service` for a ready-to-edit systemd unit. It keeps the LAN
server running across reboots; you still add media from the CLI.

## Keeping it working

YouTube changes often. If a download starts failing, update yt-dlp:

```bash
source venv/bin/activate
pip install -U yt-dlp
```

## Notes

- Only download content you have the right to. This is for personal/offline use.
- The web server has no authentication — keep it on your trusted home LAN, not
  exposed to the internet.
