<p align="center">
  <img width="290" alt="logo" src="https://github.com/user-attachments/assets/95b2ec77-52bb-42d8-b10d-793fce851dc9" />
</p>

![License](https://img.shields.io/github/license/jnracreates/ytfinall?style=flat)

# ytfinall

A companion web app for Jellyfin. Users log in with their Jellyfin
account, add YouTube channels or one-off videos, and get them downloaded
into a private Jellyfin library that only they can see.

### ↔️ Project Gallery (Scroll horizontally)

|  Login Page |  User Page |  Users Sources |  Admin login |  Admin Settings |  Advanced Admin Settings / Cookies.txt Upload |
| :---: | :---: | :---: | :---: | :---: | :---: |
| <img src="https://github.com/user-attachments/assets/9c570c89-c653-468c-b53d-87298236d656" height="400" /> | <img src="https://github.com/user-attachments/assets/3de8fd37-96f7-413b-bbb8-3d98e711fe04" height="400" /> | <img src="https://github.com/user-attachments/assets/b5d5c25d-2e20-4961-bd12-ed987ba189fd" height="400" /> | <img src="https://github.com/user-attachments/assets/fd33a7b0-8fb6-4452-a173-d6870e9e48bd" height="400" /> | <img src="https://github.com/user-attachments/assets/dc88e03a-a5bc-493b-ad8c-5056be4a5f0c" height="400" /> | <img src="https://github.com/user-attachments/assets/495a22a8-5444-408f-a829-666c1614269b" height="400" /> |





## Highlights

- First-run setup wizard — no config files to edit
- Per-user Jellyfin libraries; users keep their existing movies, TV, and music
- Admin-defined lookback window (how far back users can download)
- Admin-defined maximum retention (how long media stays before deletion)
- Drag-and-drop cookies.txt upload from the admin settings page
- yt-dlp auto-updates to nightly on container start and per request
- Atomic staging → library move — Jellyfin never sees partial files

<p align="center">
  <img width="120" alt="default-cover" src="https://github.com/user-attachments/assets/28f44d89-21cf-4cff-aedf-2726a3c846e1" />
  <br>
  <em>Custom animated GIF poster applied to each user's library</em>
</p>

## Sources

- YouTube channels, playlists, and single-video URLs
- Channel URLs are auto-normalized to `/videos` so Shorts and Streams
  tabs aren't scanned (faster, fewer API calls)
- Single videos land in a shared `One-Off Videos` folder inside the
  user's library
- Blank cutoff = last 7 days

## Downloads

- 1440p max by default (configurable 144p–8K)
- MP4 container (configurable to MKV)
- Jellyfin-friendly filenames, NFO, embedded metadata, thumbnails
- Archive tracking so repeat scans skip already-downloaded videos
- Rate-limit protection with configurable sleep intervals
- Downloads respect the admin lookback and the per-source cutoff

## Retention and cleanup

- Per-source retention (1–N days, capped by the admin's maximum)
- Automatic cleanup every 24 hours
- Empty folders are pruned after cleanup — expired channels disappear
  from Jellyfin too
- Deleting a library in Jellyfin and logging in again recreates it

## Jellyfin integration

- Creates a private `ytfinall - <username>` library on first login
- Real-time monitoring enabled
- Users keep access to their existing Jellyfin libraries
- Users never see each other's ytfinall libraries

## Admin settings

- Lookback window and maximum retention
- Playlist scan limit
- Sleep intervals (requests / between-videos min and max)
- Max resolution and media container
- Output template and extra yt-dlp arguments
- Index interval (default 12h) and cleanup interval (default 24h)
- Cookies upload (unlock via the API key first)

## Optional: cookies

Age-restricted and members-only videos need a `cookies.txt`. Upload one
from the admin settings page using the drag-and-drop box. See
`config/README.md` for export tips.

**Tip:** Export cookies from a private/incognito window and close it
immediately afterward. YouTube rotates cookies on open tabs, so a
freshly exported cookie file can go stale within minutes if the session
stays active.

## Quick start

```bash
git clone https://github.com/jnracreates/ytfinall.git
cd ytfinall
cp .env.example .env
nano .env    # set MEDIA_PATH, APP_DATA_PATH, CONFIG_PATH, JELLYFIN_CONFIG_PATH
docker compose up -d
```
Then open http://localhost:6842 and follow the setup wizard. You will
need:

    Your Jellyfin URL (as seen from inside the Docker container)

    A Jellyfin API key (Dashboard → API Keys → +)

# Docker Compose

The compose file defines a single service (ytfinall) and reads its
paths from a .env file next to it.

```yaml
# Paths are read from .env (copy .env.example to .env and edit it).
# If you'd rather not use a .env file, replace the ${VAR} entries in the
# volumes section below with literal paths on your machine. For example:
#
#   - ${MEDIA_PATH}:/media/users
# becomes
#   - /your/media/path:/media/users
#
# Both approaches work identically.
services:
  ytfinall:
    image: ghcr.io/jnracreates/ytfinall:latest
    # For local development, comment out `image:` above and uncomment:
    # build: .
    container_name: ytfinall
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ${MEDIA_PATH}:/media/users
      - ${APP_DATA_PATH}:/app-data
      - ${CONFIG_PATH}:/config
      - ${JELLYFIN_CONFIG_PATH}:/jellyfin-config
    ports:
      - "6842:6842"
    restart: unless-stopped
```

The matching .env.example:

```yaml
MEDIA_PATH=/path/to/your/media/ytfinall
APP_DATA_PATH=/path/to/your/ytfinall-app-data
CONFIG_PATH=/path/to/your/ytfinall-config
JELLYFIN_CONFIG_PATH=/path/to/your/jellyfin/config
```
```yaml
What each volume does
Variable	           Container path	   Purpose
MEDIA_PATH	         /media/users	     Downloaded media (must be shared with Jellyfin)
APP_DATA_PATH	       /app-data	       Database, staging area, per-user archives
CONFIG_PATH	         /config	         cookies.txt
JELLYFIN_CONFIG_PATH /jellyfin-config	 Jellyfin's config folder (for library posters)

MEDIA_PATH must also be mounted into your Jellyfin container at the
same container path (/media/users) so Jellyfin can see what ytfinall
downloads.
```
 ## Networking

The default compose uses Docker's bridge network and reaches Jellyfin at
http://host.docker.internal:8096. This works whether your Jellyfin
container is on the default bridge network or on network_mode: host.

If Jellyfin runs on a different machine, use that machine's LAN IP
instead (e.g. http://192.168.1.50:8096).

## Ports

ytfinall listens on **6842** by default and is reachable at
`http://your-host:6842`.

To serve it on a different host port without touching the app, change
the **left** side of the `ports:` mapping in `docker-compose.yml`:

```yaml
    ports:
      - "9000:6842"   # host:container
```

# Sharing media with Jellyfin

Add this line to your Jellyfin compose's volumes: section:
 ```yaml     
 - /path/to/ytfinall/media:/media/users
```
The right side must be exactly /media/users. The left side is where
ytfinall's MEDIA_PATH lives on your host.

## Updating
```yaml
cd ytfinall
git pull
docker compose pull
docker compose up -d
```
If you run from the source tree (with build: . in the compose),
replace docker compose pull with docker compose build.

## Browser Extension

A companion browser extension lets you right-click any YouTube video, channel, or playlist and send it straight to your ytfinall server without copy-pasting.

**Install:**

- **Microsoft Edge Add-ons** — [ytfinall Connector](https://microsoftedge.microsoft.com/addons/detail/ytfinall-connector/hdfpngaekagheekoajkjhdjoomppmlhj)
- **Firefox Add-ons (AMO)** — pending review
- **Chrome Web Store** — not available (extension download policy). See manual install below.

**Works on:**
- Microsoft Edge (desktop and mobile)
- Firefox (desktop and Android — pending AMO approval)
- Any Chromium browser that can install from the Edge Add-ons store

**Mobile note:** Right-click menus don't exist on mobile browsers. On mobile, tap the extension icon and paste the YouTube URL into the popup instead.

**How it works:**
1. Click the extension icon and log in with your Jellyfin credentials
2. Right-click any YouTube link (or paste a URL on mobile)
3. The URL is sent to your ytfinall server and queued for download
4. The video appears in your Jellyfin library when the download completes

### Chrome — manual install

The Chrome Web Store does not accept extensions that facilitate YouTube downloads. To install manually:

1. Clone the repository:

### Notes

    Videos are always limited to the admin's lookback window. A user's
    per-source cutoff can be tighter, never wider.

    Retention is capped by the admin's maximum. Users can request shorter
    retention, never longer.

    The first user to log in becomes the ytfinall admin and sees the
    Settings link in the header.

    yt-dlp tracks a per-user archive. If a file is deleted by retention,
    its ID stays in the archive — the video will not be re-downloaded
    unless the archive is cleared.

### License

## License

[MIT](LICENSE)

## Support this project

If you find ytfinall useful and want to say thanks:

- [GitHub Sponsors](https://github.com/sponsors/jnracreates) — monthly or one-time
- [Ko-fi](https://ko-fi.com/jnracreates) — one-time
- [Buy Me a Coffee](https://buymeacoffee.com/jnracreates) — one-time
