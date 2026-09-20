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

Then open `http://localhost:6842` and follow the setup wizard. Use your ip:6842 (192.*.*.*:6842)
