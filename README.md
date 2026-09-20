# ytfinall

Companion web app for Jellyfin: users log in with their Jellyfin account,
add YouTube channels and one off videos, and get videos downloaded into a **private**
Jellyfin library only they can see.

Recommend having Jellyfin Youtube Metadata Plugin installed
https://github.com/ankenyr/jellyfin-youtube-metadata-plugin

- First‑run setup wizard (no `.env` files)
- Admin defines the maximum lookback and maximum retention
- Per‑user Jellyfin libraries; each user keeps their existing libraries
- Downloads capped to the admin‑set lookback (last N days)
- yt-dlp auto‑updates on container start and (throttled) per request
- Jellyfin‑friendly filenames, NFO, embedded metadata, thumbnails
- 1440p max, MP4, no Shorts, livestreams included
- Optional `config/cookies.txt` for restricted downloads, check Readme in config folder.
- Channel re‑index every 12 hours
- Downloads land in a staging area, then move atomically — no partial
  files ever appear in the Jellyfin library

## Quick start

```bash
git clone https://github.com/jnracreates/ytfinall.git
cd ytfinall
cp .env.example .env
nano .env    # set MEDIA_PATH, APP_DATA_PATH, CONFIG_PATH, JELLYFIN_CONFIG_PATH
docker compose up -d
```
### Then open 
`http://localhost:6842` and follow the setup wizard. Use your ip:6842 (192.*.*.*:6842)

#### docker-compose.yaml with secrets
```yaml
# .env.example
# Copy this file to .env and fill in your own paths.
#
# MEDIA_PATH: where ytfinall stores downloaded videos.
#   This folder must also be mounted into Jellyfin at the same path.
#   It should be on a drive with room for media.
#
# APP_DATA_PATH: ytfinall's database, staging area, and per-user archives.
#   Small; a few hundred MB at most. Can live on your system drive.
#
# CONFIG_PATH: cookies.txt lives here.
#   Small. Can live next to APP_DATA_PATH.
#
# JELLYFIN_CONFIG_PATH: your Jellyfin container's /config folder.
#   Needed so ytfinall can write library posters directly.
#   Point this at the same host folder your Jellyfin container mounts
#   to /config.

MEDIA_PATH=/path/to/your/media/ytfinall
APP_DATA_PATH=/path/to/your/ytfinall-app-data
CONFIG_PATH=/path/to/your/ytfinall-config
JELLYFIN_CONFIG_PATH=/path/to/your/jellyfin/config
```

```yaml
services:
  ytfinall:    
    image: ghcr.io/jnracreates/ytfinall:latest
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
##### docker-compose.yaml NO secrets


```yaml
services:
  ytfinall:    
    image: ghcr.io/jnracreates/ytfinall:latest
    container_name: ytfinall
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - /path_to_media:/media/users   # make sure jellyfin has access to the location and you have a users folder there.
      - /path_to_ytfinall/app-data:/app-data
      - /path_to_ytfinall/config:/config
      - /path_to_jellyfin_config:/jellyfin-config # needed to add poster to created folder and library.
    ports:
      - "6842:6842"
    restart: unless-stopped
```
# Step by Step Easy guide

Create ytfinall folder at your location of choice.

Create config,static & app-data folder inside of it.

Create .env file if using secrets. it will be in the ytfinall folder.

Create a docker-compose.yaml file from one of the options above. it will be in the ytfinall folder.

Edit docker-compose.yaml with your correct paths.

Make sure you are in the ytfinall folder.

Run
```bash
docker compose pull
docker compose up -d
```

Then open
http://localhost:6842 and follow the setup wizard. Use your ip:6842 (192...*:6842)
