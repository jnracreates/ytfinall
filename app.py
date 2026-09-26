import os, sqlite3, subprocess, threading, time, datetime, requests, shutil, re
from flask import Flask, request, redirect, render_template_string, session, jsonify, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Owner-only permissions for everything this process creates.
os.umask(0o077)

def apply_animated_library_cover(user_media_folder):
    """
    Copies the local default YouTube GIF from the static folder into the user's
    media library so Jellyfin displays it automatically as an animated thumbnail.
    """
    os.makedirs(user_media_folder, exist_ok=True)

    # Path where Jellyfin expects the image file
    gif_destination = os.path.join(user_media_folder, "poster.gif")

    # Relative path pointing to the master copy inside your public static folder
    master_gif_source = "static/default-cover.gif"

    # Copy the file dynamically if it doesn't exist in the target folder yet
    if not os.path.exists(gif_destination):
        if os.path.exists(master_gif_source):
            try:
                shutil.copy2(master_gif_source, gif_destination)
                print(f"✨ Successfully cloned animated GIF cover to: {gif_destination}")
            except Exception as e:
                print(f"⚠️ Failed to copy library artwork file: {e}")
        else:
            print(f"⚠️ Master artwork asset not found at {master_gif_source}. Please verify it is in your static folder.")


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB body cap
app.jinja_env.autoescape = True  # render_template_string does NOT autoescape
# Restrict CORS to your extension's origin. Replace the placeholders
# with your real IDs, or leave as-is if you don't have an extension yet
# (the web UI is same-origin and unaffected by CORS).
CORS(app, resources={r"/api/*": {"origins": [
    "chrome-extension://REPLACE_WITH_CHROME_EXTENSION_ID",
    "moz-extension://REPLACE_WITH_FIREFOX_EXTENSION_ID",
]}})
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)


# ------------------------------------------------------------------
# In-memory log buffer for the admin live-logs panel
# ------------------------------------------------------------------
import sys as _sys
import threading as _threading
from collections import deque as _deque

_log_buffer = _deque(maxlen=500)
_log_lock = _threading.Lock()


class _TeeStdout:
    """Write to the real stdout AND append complete lines to the ring buffer.

    print() calls write() twice: once with the text (no newline) and once
    with just the newline. We accumulate partial writes until a newline
    arrives, then emit the complete line.
    """
    def __init__(self, original):
        self.original = original
        self._pending = ""

    def write(self, data):
        if not data:
            return
        text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else data
        try:
            self.original.write(data)
        except Exception:
            pass
        combined = self._pending + text
        if "\n" not in combined:
            self._pending = combined
            return
        *complete, self._pending = combined.split("\n")
        with _log_lock:
            for line in complete:
                line = line.rstrip()
                if not line.strip():
                    continue
                if "/admin/logs" in line:
                    continue
                # Redact query strings and auth tokens before exposing
                # to the admin UI. Raw stdout still gets the original.
                redacted = re.sub(r"(\?|&)[^\s]*", r"\1<redacted>", line)
                redacted = re.sub(r'Token="[^"]+"', 'Token="<redacted>"', redacted)
                _log_buffer.append(redacted)

    def flush(self):
        # Emit any dangling partial line so it isn't lost on shutdown
        if self._pending.strip():
            with _log_lock:
                _log_buffer.append(self._pending)
            self._pending = ""
        try:
            self.original.flush()
        except Exception:
            pass

    def isatty(self):
        return False


if not isinstance(_sys.stdout, _TeeStdout):
    _sys.stdout = _TeeStdout(_sys.stdout)
if not isinstance(_sys.stderr, _TeeStdout):
    _sys.stderr = _TeeStdout(_sys.stderr)

MEDIA_ROOT = "/media/users"
STAGING_ROOT = "/app-data/staging"
CONFIG_DIR = "/config"
COOKIES_FILE = os.path.join(CONFIG_DIR, "cookies.txt")
DB_PATH = "/app-data/ytfinall.db"
YT_LIB_PREFIX = "ytfinall - "


def safe_username(name):
    """Turn a Jellyfin username into a folder-safe string."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "user").strip()
    cleaned = cleaned.strip(". ")  # Windows dislikes trailing dots/spaces
    return cleaned or "user"


def is_single_video_url(url):
    """True if the URL is a single video (not a channel or playlist)."""
    url = url or ""
    is_playlist_only = (
        "/playlist" in url
        or ("list=" in url and "watch?v=" not in url and "youtu.be/" not in url)
    )
    return (
        ("youtube.com/watch" in url or "youtu.be/" in url)
        and not is_playlist_only
    )

os.makedirs("/app-data", exist_ok=True)
os.makedirs(MEDIA_ROOT, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(STAGING_ROOT, exist_ok=True)

# Clean up stale staging folders older than 24h from prior crashes.
try:
    _now = time.time()
    for _user_dir in os.listdir(STAGING_ROOT):
        _up = os.path.join(STAGING_ROOT, _user_dir)
        if os.path.isdir(_up) and _now - os.path.getmtime(_up) > 86400:
            shutil.rmtree(_up, ignore_errors=True)
except Exception as _e:
    print(f"[startup] staging cleanup error: {_e}")

# Note: the download_tasks cleanup that used to run here was moved into
# init_db(), which already does the same thing once the DB layer is up.
# Running it here failed because db() isn't defined yet at this point.

# Persist the Flask session secret so logins survive restarts.
_secret_path = "/app-data/secret.key"
if os.path.exists(_secret_path):
    with open(_secret_path) as f:
        app.secret_key = f.read().strip()
else:
    _key = os.urandom(32).hex()
    with open(_secret_path, "w") as f:
        f.write(_key)
    app.secret_key = _key


# ------------------------------------------------------------------
# Database
# ------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                url TEXT NOT NULL,
                name TEXT,
                cutoff TEXT,
                retention_days INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                library_id TEXT,
                is_admin INTEGER DEFAULT 0,
                delete_on_finish INTEGER DEFAULT 0
            )
        """)
        # Idempotent migrations for DBs created before these columns existed.
        try:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE users ADD COLUMN delete_on_finish INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Opaque tokens issued to the browser extension. Only hashes
        # are stored, so a DB read does not yield usable credentials.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_tokens (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                username TEXT,
                created_at TEXT,
                expires_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                url TEXT NOT NULL,
                kind TEXT,
                status TEXT,
                progress_pct REAL DEFAULT 0,
                current_item INTEGER DEFAULT 0,
                total_items INTEGER DEFAULT 0,
                downloaded_count INTEGER DEFAULT 0,
                eta_seconds INTEGER,
                message TEXT,
                started_at TEXT,
                updated_at TEXT,
                finished_at TEXT
            )
        """)
        # Any task not in a terminal state at startup is orphaned —
        # the process that owned it is gone. Mark them failed so the
        # dashboard doesn't show ghost "active" entries.
        _startup_now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        _cur = conn.execute(
            "UPDATE download_tasks SET status='failed', "
            "message='Interrupted by server restart', "
            "finished_at=? "
            "WHERE status NOT IN ('complete', 'failed')",
            (_startup_now,),
        )
        if _cur.rowcount:
            print(f"[startup] cleared {_cur.rowcount} orphaned task(s)",
                  flush=True)


init_db()


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def create_task(user_id, url, kind):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO download_tasks "
            "(user_id, url, kind, status, started_at, updated_at, message) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (user_id, url, kind, _now_iso(), _now_iso(), "Queued"),
        )
        return cur.lastrowid


def update_task(task_id, **fields):
    if not fields:
        return
    fields["updated_at"] = _now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    with db() as conn:
        conn.execute(f"UPDATE download_tasks SET {cols} WHERE id=?", vals)


def finish_task(task_id, status, message):
    update_task(task_id, status=status, message=message,
                finished_at=_now_iso())


def get_user_tasks(user_id, limit=5):
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM download_tasks WHERE user_id=? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def purge_old_tasks(user_id, age_seconds=600):
    cutoff = (datetime.datetime.now(datetime.UTC)
              - datetime.timedelta(seconds=age_seconds)
              ).isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            "DELETE FROM download_tasks "
            "WHERE user_id=? AND finished_at IS NOT NULL AND finished_at < ?",
            (user_id, cutoff),
        )


def get_config(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key, value):
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, str(value)),
        )


def is_configured():
    return bool(get_config("jellyfin_api_key"))


def jellyfin_url():
    return get_config("jellyfin_url", "http://host.docker.internal:8096")


def jellyfin_api_key():
    return get_config("jellyfin_api_key", "")


def webhook_secret():
    """Shared secret appended to the Jellyfin webhook URL.

    Generated once and stored in the config table. Jellyfin's Webhook plugin
    doesn't support custom headers on every version, so the token rides in
    the query string.
    """
    s = get_config("webhook_secret")
    if not s:
        import secrets
        s = secrets.token_urlsafe(24)
        set_config("webhook_secret", s)
    return s


# Expose to templates so SETTINGS_PAGE can render the URL without every
# render_template_string() call having to pass it explicitly.
app.jinja_env.globals["webhook_secret"] = webhook_secret


def max_lookback_days():
    return int(get_config("max_lookback_days", 31))


def max_retention_days():
    return int(get_config("max_retention_days", 31))


def playlist_end():
    return int(get_config("playlist_end", 35))


def sleep_requests():
    return int(get_config("sleep_requests", 3))


def sleep_interval():
    return int(get_config("sleep_interval", 20))


def max_sleep_interval():
    return int(get_config("max_sleep_interval", 60))


def max_resolution():
    v = int(get_config("max_resolution", 1440))
    return max(144, min(v, 4320))


def media_container():
    return get_config("media_container", "mp4")


def outtmpl_setting():
    return get_config(
        "outtmpl",
        "%(channel)s [%(channel_id)s]/"
        "Season %(upload_date>%Y)s/"
        "%(channel)s - s%(upload_date>%Y)se%(upload_date>%m%d)s - %(title)s [%(id)s].%(ext)s",
    )


def extra_ytdlp_args():
    raw = get_config("extra_ytdlp_args", "")
    return raw.split() if raw else []


DONATION_MESSAGE = (
    "If you enjoy what I do, consider supporting me! "
    "Every little bit means the world!"
)
DONATION_LINKS = [
    ("Support via Stripe", "https://buy.stripe.com/aFa00i5cia9M6Jt5y5eME00"),
    ("Ko-fi", "https://ko-fi.com/jnracreates"),
    ("Buy Me a Coffee", "https://buymeacoffee.com/jnracreates"),
]


def index_interval_hours():
    return int(get_config("index_interval_hours", 12))


def cleanup_interval_hours():
    return int(get_config("cleanup_interval_hours", 24))


def archive_stats(user_id):
    """Return (entry_count, file_exists) for a user's download archive."""
    path = f"/app-data/{user_id}/archive.txt"
    if not os.path.exists(path):
        return 0, False
    with open(path) as f:
        return sum(1 for line in f if line.strip()), True


def get_user_stats(username):
    """Walk a user's media folder and return video count + total bytes."""
    safe = safe_username(username)
    root = f"{MEDIA_ROOT}/{safe}/shows"
    if not os.path.isdir(root):
        return {"videos": 0, "bytes": 0, "folders": 0}
    media_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v",
                  ".mp3", ".m4a", ".flac", ".opus", ".ogg"}
    videos = 0
    total = 0
    folders = 0
    for dirpath, dirnames, filenames in os.walk(root):
        folders += 1
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext in media_exts:
                try:
                    size = os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    continue
                videos += 1
                total += size
    return {"videos": videos, "bytes": total, "folders": folders}


def _fmt_bytes(n):
    """Format bytes as a human-readable string."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} TB"


def build_user_list():
    """Return a list of users with their source and archive counts."""
    result = []
    with db() as conn:
        users = conn.execute("SELECT user_id, username FROM users").fetchall()
        for u in users:
            src_count = conn.execute(
                "SELECT COUNT(*) AS c FROM sources WHERE user_id=?",
                (u["user_id"],),
            ).fetchone()["c"]
            arc_count, _ = archive_stats(u["user_id"])
            result.append({
                "user_id": u["user_id"],
                "username": u["username"] or u["user_id"],
                "sources": src_count,
                "archive_count": arc_count,
            })
    return result


# ------------------------------------------------------------------
# YouTube search — find videos/channels without downloading
# ------------------------------------------------------------------
import json as _json
import urllib.parse as _urlparse

_search_cache = {}                 # key -> (expires_at, payload)
_search_cache_lock = threading.Lock()
_SEARCH_TTL = 300                  # seconds


def _fmt_duration(seconds):
    if not seconds:
        return ""
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _search_videos(query, limit=15):
    """yt-dlp ytsearch in flat-playlist mode. No download, no resolve."""
    cmd = [
        "yt-dlp", f"ytsearch{limit}:{query}",
        "--flat-playlist", "--dump-json",
        "--no-warnings", "--skip-download",
        "--extractor-args",
        "youtube:player_client=android,web_embedded,-visionos",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        print(f"[search] ytsearch timeout for {query!r}", flush=True)
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = _json.loads(line)
        except ValueError:
            continue
        vid = d.get("id") or ""
        url = d.get("url") or ""
        if url and not url.startswith("http") and vid:
            url = f"https://www.youtube.com/watch?v={vid}"
        thumb = d.get("thumbnail")
        thumbs = d.get("thumbnails") or []
        if not thumb and thumbs:
            thumb = thumbs[-1].get("url")
        out.append({
            "kind": "video",
            "id": vid,
            "url": url,
            "title": d.get("title") or "",
            "channel": d.get("channel") or d.get("uploader") or "",
            "channel_id": d.get("channel_id") or d.get("uploader_id") or "",
            "duration_human": _fmt_duration(d.get("duration")),
            "thumbnail": thumb,
            "live": bool(d.get("is_live")),
        })
    return out


def _search_channels(query, limit=12):
    """Scrape YouTube's channel search results. No API key needed."""
    url = (
        "https://www.youtube.com/results?search_query="
        + _urlparse.quote(query)
        + "&sp=EgIQAg%253D%253D"   # "Channels" filter
    )
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "20",
             "-H", "Accept-Language: en-US,en;q=0.9",
             url],
            capture_output=True, text=True, timeout=25,
        )
    except Exception as e:
        print(f"[search] channel curl error: {e}", flush=True)
        return []
    if r.returncode != 0 or not r.stdout:
        return []

    m = re.search(r"var ytInitialData\s*=\s*(\{.+?\});</script>", r.stdout)
    if not m:
        return []
    try:
        data = _json.loads(m.group(1))
    except ValueError:
        return []

    found = []

    def walk(node):
        if isinstance(node, dict):
            if "channelRenderer" in node:
                found.append(node["channelRenderer"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)

    out = []
    for ch in found[:limit]:
        try:
            cid = ch.get("channelId") or ""
            if not cid:
                continue
            title = (ch.get("title") or {}).get("simpleText") or ""
            if not title:
                runs = (ch.get("title") or {}).get("runs") or []
                title = "".join(r.get("text", "") for r in runs)
            subs = (ch.get("videoCountText") or {}).get("simpleText") or ""
            if not subs:
                subs = (ch.get("subscriberCountText") or {}).get("simpleText") or ""
            desc_runs = ((ch.get("descriptionSnippet") or {}).get("runs")) or []
            desc = "".join(r.get("text", "") for r in desc_runs)
            thumb = ""
            t = (ch.get("thumbnail") or {}).get("thumbnails") or []
            if t:
                thumb = t[-1].get("url") or ""
            out.append({
                "kind": "channel",
                "id": cid,
                "url": f"https://www.youtube.com/channel/{cid}",
                "title": title,
                "channel": title,
                "subscribers": subs,
                "description": desc,
                "thumbnail": thumb,
            })
        except Exception:
            continue
    return out


def cached_search(query, kind):
    key = (kind, query.lower().strip())
    now = time.time()
    with _search_cache_lock:
        hit = _search_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    payload = _search_channels(query) if kind == "channel" else _search_videos(query)
    with _search_cache_lock:
        _search_cache[key] = (now + _SEARCH_TTL, payload)
        if len(_search_cache) > 100:
            for k in list(_search_cache)[:50]:
                _search_cache.pop(k, None)
    return payload


def _list_channel_videos(channel_id, count=30):
    """List up to `count` videos from a channel via yt-dlp flat-playlist."""
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    cmd = [
        "yt-dlp", url,
        "--flat-playlist", "--dump-json",
        "--no-warnings", "--skip-download",
        "--playlist-end", str(count),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        print(f"[channel] timeout for {channel_id}", flush=True)
        return []
    out = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = _json.loads(line)
        except ValueError:
            continue
        vid = d.get("id") or ""
        if not vid:
            continue
        thumb = d.get("thumbnail")
        thumbs = d.get("thumbnails") or []
        if not thumb and thumbs:
            thumb = thumbs[-1].get("url")
        out.append({
            "kind": "video",
            "id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": d.get("title") or "",
            "channel": d.get("channel") or d.get("uploader") or "",
            "channel_id": d.get("channel_id") or d.get("uploader_id") or channel_id,
            "duration_human": _fmt_duration(d.get("duration")),
            "thumbnail": thumb,
        })
    return out


def _channel_info(channel_id):
    """Scrape channel name + avatar from the channel page."""
    try:
        r = subprocess.run(
            ["curl", "-sL", "--max-time", "20",
             "-H", "Accept-Language: en-US,en;q=0.9",
             f"https://www.youtube.com/channel/{channel_id}"],
            capture_output=True, text=True, timeout=25,
        )
    except Exception:
        return {}
    if r.returncode != 0 or not r.stdout:
        return {}
    avatar = ""
    m = re.search(r'https://yt3\.googleusercontent\.com/[a-zA-Z0-9=_-]+', r.stdout)
    if m:
        avatar = m.group(0)
    title = ""
    m = re.search(r'"channelMetadataRenderer":\{"title":"([^"]+)"', r.stdout)
    if m:
        title = m.group(1)
    else:
        m = re.search(r'<meta property="og:title" content="([^"]+)"', r.stdout)
        if m:
            title = m.group(1)
    return {"title": title, "thumbnail": avatar}


# ------------------------------------------------------------------
# yt-dlp auto-update (throttled)
# ------------------------------------------------------------------
_last_update = 0.0
_update_lock = threading.Lock()
_download_lock = threading.Lock()

# Bounded download queue with a fixed worker pool. Prevents unbounded
# thread spawning under load.
import queue as _queue
_download_queue: "_queue.Queue[tuple]" = _queue.Queue(maxsize=32)


def _download_worker():
    while True:
        job = _download_queue.get()
        try:
            user_id, url, name, cutoff = job
            run_download(user_id, url, name, cutoff)
        except Exception as e:
            print(f"[queue] worker error: {e}", flush=True)
        finally:
            _download_queue.task_done()


for _ in range(2):
    threading.Thread(target=_download_worker, daemon=True).start()


def enqueue_download(user_id, url, name=None, cutoff=None) -> bool:
    """Return False if the queue is full."""
    try:
        _download_queue.put_nowait((user_id, url, name, cutoff))
        return True
    except _queue.Full:
        return False


def ensure_ytdlp_updated(force=False):
    global _last_update
    with _update_lock:
        if not force and time.time() - _last_update < 300:
            return
        print("[yt-dlp] updating...", flush=True)
        subprocess.run(
            ["pip", "install", "--upgrade", "--pre", "yt-dlp[default]"],
            check=False, capture_output=True,
        )
        _last_update = time.time()
        print("[yt-dlp] update complete", flush=True)


# ------------------------------------------------------------------
# Jellyfin helpers
# ------------------------------------------------------------------
def _auth_header():
    return (
        'MediaBrowser Client="ytfinall", Device="Server", '
        'DeviceId="ytfinall", Version="1.0"'
    )


def jellyfin_login(username, password):
    r = requests.post(
        f"{jellyfin_url()}/Users/AuthenticateByName",
        json={"Username": username, "Pw": password},
        headers={"Content-Type": "application/json",
                 "Authorization": _auth_header()},
        timeout=15,
    )
    if r.status_code == 200:
        data = r.json()
        return data["User"]["Id"], data["AccessToken"]
    return None, None


def list_jellyfin_libraries():
    """Return [{id, name}, ...] for all virtual folders using ItemId safely."""
    api_key = jellyfin_api_key()
    if not api_key:
        return []
    try:
        r = requests.get(
            f"{jellyfin_url()}/Library/VirtualFolders",
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
            timeout=20,
        )
        r.raise_for_status()
        libs = []
        for lib in r.json():
            lib_id = lib.get("ItemId")
            name = lib.get("Name") or ""
            if lib_id:
                libs.append({"id": str(lib_id), "name": name})
        return libs
    except Exception as e:
        print(f"[jellyfin] could not list libraries: {e}")
        return []


def _create_jellyfin_library(lib_name, path):
    """POST using query parameters + a LibraryOptions body."""
    api_key = jellyfin_api_key()
    url = jellyfin_url()
    headers = {
        "Authorization": (
            f'MediaBrowser Token="{api_key}", '
            f'Client="ytfinall", Device="Server", '
            f'DeviceId="ytfinall", Version="1.0"'
        ),
        "Content-Type": "application/json",
    }

    params = [
        ("name", lib_name),
        ("collectionType", "tvshows"),
        ("paths", path),
        ("refreshLibrary", "false"),
    ]

    body = {
        "LibraryOptions": {
            "EnableRealtimeMonitor": True,
            "SaveLocalMetadata": False,
            "MetadataSavers": [],
            "LocalMetadataReaderOrder": ["Nfo"],
            "DisabledLocalMetadataReaders": ["YoutubeMetadata"],
            "EnableInternetProviders": False,
            "EnableAutomaticSeriesGrouping": False,
            "PathInfos": [{"Path": path}],
            "TypeOptions": [
                {
                    "Type": "Series",
                    "MetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "MetadataFetcherOrder": ["TheMovieDb", "The Open Movie Database"],
                    "DisabledMetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "ImageFetchers": ["TheMovieDb"],
                    "ImageFetcherOrder": ["TheMovieDb"],
                    "DisabledImageFetchers": ["TheMovieDb"],
                },
                {
                    "Type": "Season",
                    "MetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "MetadataFetcherOrder": ["TheMovieDb", "The Open Movie Database"],
                    "DisabledMetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "ImageFetchers": ["TheMovieDb"],
                    "ImageFetcherOrder": ["TheMovieDb"],
                    "DisabledImageFetchers": ["TheMovieDb"],
                },
                {
                    "Type": "Episode",
                    "MetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "MetadataFetcherOrder": ["TheMovieDb", "The Open Movie Database"],
                    "DisabledMetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "ImageFetchers": ["TheMovieDb", "The Open Movie Database", "Embedded Image Extractor", "Screen Grabber"],
                    "ImageFetcherOrder": ["TheMovieDb", "The Open Movie Database", "Embedded Image Extractor", "Screen Grabber"],
                    "DisabledImageFetchers": ["TheMovieDb", "The Open Movie Database"],
                },
            ],
        }
    }

    try:
        r = requests.post(
            f"{url}/Library/VirtualFolders",
            params=params, json=body, headers=headers, timeout=30,
        )
        if r.status_code not in (200, 204):
            print(f"[jellyfin] library create returned {r.status_code}: {r.text[:400]}")
            return False
        return True
    except Exception as e:
        print(f"[jellyfin] library create error: {e}")
        return False


def _set_library_image(lib_name, image_path):
    """Write the library image directly into Jellyfin's config folder."""
    if not os.path.exists(image_path):
        print(f"[jellyfin] no image at {image_path}")
        return False

    config_dir = "/jellyfin-config"
    if not os.path.isdir(config_dir):
        print("[jellyfin] /jellyfin-config not mounted; skipping library image")
        return False

    root_dir = os.path.join(config_dir, "root", "default")
    dest_dir = os.path.join(root_dir, lib_name)

    if not os.path.isdir(dest_dir):
        print(f"[jellyfin] library folder not found: {dest_dir}")
        return False

    dest = os.path.join(dest_dir, "poster.gif")
    try:
        shutil.copy2(image_path, dest)
        print(f"[jellyfin] wrote {dest}", flush=True)
        return True
    except Exception as e:
        print(f"[jellyfin] image write error: {e}")
        return False


def _update_jellyfin_library_options(lib_id, lib_name):
    """Update TypeOptions on an existing library (create endpoint ignores them)."""
    api_key = jellyfin_api_key()
    url = jellyfin_url()
    headers = {
        "Authorization": (
            f'MediaBrowser Token="{api_key}", '
            f'Client="ytfinall", Device="Server", '
            f'DeviceId="ytfinall", Version="1.0"'
        ),
        "Content-Type": "application/json",
    }

    # Fetch the library paths so we do not wipe them on options update.
    current_paths = []
    try:
        r = requests.get(
            f"{url}/Library/VirtualFolders",
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
            timeout=15,
        )
        for lib in r.json():
            if lib.get("ItemId") == lib_id:
                paths = lib.get("Locations") or []
                seen = set()
                current_paths = [p for p in paths if not (p in seen or seen.add(p))]
                break
    except Exception as e:
        print(f"[jellyfin] could not fetch paths for options update: {e}")

    body = {
        "Id": lib_id,
        "LibraryOptions": {
            "EnableRealtimeMonitor": True,
            "SaveLocalMetadata": False,
            "MetadataSavers": [],
            "LocalMetadataReaderOrder": ["Nfo"],
            "DisabledLocalMetadataReaders": ["YoutubeMetadata"],
            "EnableInternetProviders": False,
            "EnableAutomaticSeriesGrouping": False,
            "PathInfos": [{"Path": p} for p in current_paths],
            "TypeOptions": [
                {
                    "Type": "Series",
                    "MetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "MetadataFetcherOrder": ["TheMovieDb", "The Open Movie Database"],
                    "DisabledMetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "ImageFetchers": ["TheMovieDb"],
                    "ImageFetcherOrder": ["TheMovieDb"],
                    "DisabledImageFetchers": ["TheMovieDb"],
                },
                {
                    "Type": "Season",
                    "MetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "MetadataFetcherOrder": ["TheMovieDb", "The Open Movie Database"],
                    "DisabledMetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "ImageFetchers": ["TheMovieDb"],
                    "ImageFetcherOrder": ["TheMovieDb"],
                    "DisabledImageFetchers": ["TheMovieDb"],
                },
                {
                    "Type": "Episode",
                    "MetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "MetadataFetcherOrder": ["TheMovieDb", "The Open Movie Database"],
                    "DisabledMetadataFetchers": ["TheMovieDb", "The Open Movie Database"],
                    "ImageFetchers": ["TheMovieDb", "The Open Movie Database", "Embedded Image Extractor", "Screen Grabber"],
                    "ImageFetcherOrder": ["TheMovieDb", "The Open Movie Database", "Embedded Image Extractor", "Screen Grabber"],
                    "DisabledImageFetchers": ["TheMovieDb", "The Open Movie Database"],
                },
            ],
        },
    }

    try:
        r = requests.post(
            f"{url}/Library/VirtualFolders/LibraryOptions",
            json=body, headers=headers, timeout=30,
        )
        if r.status_code not in (200, 204):
            print(f"[jellyfin] options update returned {r.status_code}: {r.text[:400]}")
            return False
        print(f"[jellyfin] options updated for library {lib_id}", flush=True)
        return True
    except Exception as e:
        print(f"[jellyfin] options update error: {e}")
        return False


def ensure_user_library(user_id, username):
    api_key = jellyfin_api_key()
    if not api_key:
        print("[jellyfin] no API key configured; skipping library creation")
        return None

    url = jellyfin_url()
    headers = {
        "Authorization": (
            f'MediaBrowser Token="{api_key}", '
            f'Client="ytfinall", Device="Server", '
            f'DeviceId="ytfinall", Version="1.0"'
        )
    }

    # --- 1. Create this user's library if needed ---------------------
    with db() as conn:
        row = conn.execute(
            "SELECT library_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        my_lib_id = row["library_id"] if row and row["library_id"] else None

    # Verify the cached library still exists in Jellyfin. If it was deleted
    # (or renamed), drop the cache so we recreate it below.
    if my_lib_id:
        known = {lib["id"] for lib in list_jellyfin_libraries()}
        if my_lib_id not in known:
            print(f"[jellyfin] cached library {my_lib_id} no longer exists, "
                  f"will recreate", flush=True)
            my_lib_id = None
            with db() as conn:
                conn.execute(
                    "UPDATE users SET library_id=NULL WHERE user_id=?",
                    (user_id,),
                )


    safe_name = safe_username(username)
    lib_name = f"{YT_LIB_PREFIX}{safe_name}"
    user_shows = f"{MEDIA_ROOT}/{safe_name}/shows"

    # Before creating, check if a library with this exact name already
    # exists in Jellyfin. This catches the case where our cached ID is
    # stale but the library itself is fine, and prevents duplicate
    # creation (jnra, jnra2, jnra3, ...).
    if not my_lib_id:
        existing = next(
            (l for l in list_jellyfin_libraries() if l["name"] == lib_name),
            None,
        )
        if existing:
            my_lib_id = existing["id"]
            with db() as conn:
                conn.execute(
                    "UPDATE users SET library_id=? WHERE user_id=?",
                    (my_lib_id, user_id),
                )
            print(f"[jellyfin] recovered existing library by name: "
                  f"{lib_name} -> {my_lib_id}", flush=True)

    if not my_lib_id:
        os.makedirs(user_shows, exist_ok=True)

        if not _create_jellyfin_library(lib_name, user_shows):
            return None

        # Poll — Jellyfin writes the new library asynchronously.
        for _ in range(5):
            time.sleep(0.5)
            for lib in list_jellyfin_libraries():
                if lib["name"] == lib_name:
                    my_lib_id = lib["id"]
                    break
            if my_lib_id:
                break

        if not my_lib_id:
            print("[jellyfin] could not find library id after creation loops")
            return None

        with db() as conn:
            existing = conn.execute(
                "SELECT is_admin FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
            is_admin = existing["is_admin"] if existing else 0
            conn.execute(
                "INSERT OR REPLACE INTO users (user_id, username, library_id, is_admin) "
                "VALUES (?,?,?,?)",
                (user_id, username, my_lib_id, is_admin),
            )

        # The create endpoint ignores TypeOptions, so update them separately.
        _update_jellyfin_library_options(my_lib_id, lib_name)

    # Always ensure the library image is set (runs on every login).
    if my_lib_id:
        _set_library_image(lib_name, "/config/default-cover.gif")

    # --- 2. Skip policy rewrite for Jellyfin admins ------------------
    try:
        r = requests.get(f"{url}/Users/{user_id}", headers=headers, timeout=20)
        r.raise_for_status()
        user_obj = r.json()
        policy = user_obj.get("Policy", {})
        if policy.get("IsAdministrator"):
            print(f"[jellyfin] {username} is a Jellyfin admin; skipping policy update")
            return my_lib_id
    except Exception as e:
        print(f"[jellyfin] failed to fetch user metadata: {e}")
        return my_lib_id

    # --- 3. Update the user's policy safely --------------------------
    try:
        all_libs = list_jellyfin_libraries()

        visible_ids = []
        for lib in all_libs:
            if lib["name"].startswith(YT_LIB_PREFIX):
                if lib["id"] == str(my_lib_id):
                    visible_ids.append(lib["id"])
            else:
                visible_ids.append(lib["id"])

        existing = set(str(i) for i in policy.get("EnabledFolders") or [])
        for lib_id in existing:
            if lib_id in visible_ids:
                continue
            match = next((l for l in all_libs if l["id"] == lib_id), None)
            if match and not match["name"].startswith(YT_LIB_PREFIX):
                visible_ids.append(lib_id)

        policy["EnableAllFolders"] = False
        policy["EnabledFolders"] = visible_ids

        update_resp = requests.post(
            f"{url}/Users/{user_id}/Policy",
            json=policy, headers=headers, timeout=20,
        )
        update_resp.raise_for_status()
        print(f"[jellyfin] policy updated for {username}: "
              f"{len(visible_ids)} libraries enabled")
    except Exception as e:
        print(f"[jellyfin] policy update error: {e}")

    return my_lib_id


def refresh_jellyfin_library(lib_id):
    api_key = jellyfin_api_key()
    if not lib_id or not api_key:
        return
    try:
        requests.post(
            f"{jellyfin_url()}/Items/{lib_id}/Refresh",
            params={"Recursive": "true",
                    "ImageRefreshMode": "Default",
                    "MetadataRefreshMode": "Default"},
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'}, timeout=20,
        )
    except Exception as e:
        print(f"[jellyfin] refresh error: {e}")


# ------------------------------------------------------------------
# yt-dlp command builder — always limited to the admin's lookback
# ------------------------------------------------------------------
def build_ytdlp_cmd(user_id, url, custom_name=None, cutoff_date=None):
    # If it's a channel root, force the /videos tab so we don't scan
    # streams/shorts separately (which triples the number of requests).
    # A watch URL is a single video even if it has a &list= parameter.
    # Only treat pure playlist URLs (no v= parameter) as channels/playlists.
    is_playlist_only = (
        "/playlist" in url
        or ("list=" in url and "watch?v=" not in url and "youtu.be/" not in url)
    )
    is_single_video = (
        ("youtube.com/watch" in url or "youtu.be/" in url)
        and not is_playlist_only
    )

    if not is_single_video and "youtube.com/@" in url and not any(
        tab in url for tab in ("/videos", "/shorts", "/streams", "/playlists")
    ):
        url = url.rstrip("/") + "/videos"

    today = datetime.date.today()
    admin_min = today - datetime.timedelta(days=max_lookback_days())
    default_min = today - datetime.timedelta(days=7)

    if cutoff_date:
        requested = None
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                requested = datetime.datetime.strptime(cutoff_date, fmt).date()
                break
            except ValueError:
                continue
        if requested:
            # Never go earlier than the admin's limit
            effective = max(requested, admin_min)
        else:
            print(f"[download] could not parse cutoff '{cutoff_date}', "
                  f"falling back to {default_min}", flush=True)
            effective = default_min
    else:
        # Blank cutoff = last 7 days
        effective = default_min

    dateafter = effective.strftime("%Y%m%d")

    # Unique staging directory per download so parallel jobs don't collide.
    staging_dir = f"{STAGING_ROOT}/{user_id}/{int(time.time() * 1000)}"
    os.makedirs(staging_dir, exist_ok=True)

    # Look up the username so media lands in /media/users/<username>/shows
    with db() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    username = safe_username(row["username"]) if row and row["username"] else user_id

    if is_single_video:
        outtmpl = (
            f"{staging_dir}/"
            "One-Off Videos/"
            "Season %(upload_date>%Y)s/"
            "One-Off Videos - s%(upload_date>%Y)se%(upload_date>%m%d)s - %(title)s [%(id)s].%(ext)s"
        )
    else:
        outtmpl = f"{staging_dir}/{outtmpl_setting()}"

    archive = f"/app-data/{user_id}/archive.txt"
    os.makedirs(os.path.dirname(archive), exist_ok=True)

    res = max_resolution()
    container = media_container()

    cmd = [
        "yt-dlp",
        "--playlist-end", str(playlist_end()),
        "--download-archive", archive,
        "--break-on-existing",
        "--write-thumbnail", "--embed-thumbnail",
        "--write-info-json", "--embed-metadata",
        "--no-write-playlist-metafiles",
        "--live-from-start",
        "-f", f"bestvideo[height<={res}]+bestaudio/best[height<={res}]",
        "--merge-output-format", container,
        "--extractor-args", "youtube:player_client=android,web_embedded,-visionos",
        "--sleep-requests", str(sleep_requests()),
        "--sleep-interval", str(sleep_interval()),
        "--max-sleep-interval", str(max_sleep_interval()),
        "--ignore-errors",
    ]
    if not is_single_video:
        cmd += ["--match-filter", f"upload_date >= {dateafter} & aspect_ratio>=1"]
        cmd += ["--break-on-reject"]
    else:
        # Single video: ignore any &list= playlist context in the URL
        cmd += ["--no-playlist"]

    cmd += extra_ytdlp_args()
    cmd += ["-o", outtmpl]

    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]

    cmd += [url]
    return cmd, staging_dir


def _merge_move(src, dst):
    """Recursively move everything from src into dst, merging directories."""
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        s = os.path.join(src, entry)
        d = os.path.join(dst, entry)
        if os.path.isdir(s):
            _merge_move(s, d)
        else:
            if os.path.exists(d):
                os.remove(d)
            shutil.move(s, d)


def write_episode_nfo_from_json(info_json_path, max_paragraphs=2):
    """
    Read a yt-dlp .info.json file and write a sibling .nfo that Jellyfin
    can parse. Trims the description to max_paragraphs for readability.
    """
    import json

    try:
        with open(info_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[nfo] could not read {info_json_path}: {e}", flush=True)
        return

    # Derive the .nfo path from the .info.json path
    nfo_path = info_json_path[:-len(".info.json")] + ".nfo"

    # Skip if NFO already exists
    if os.path.exists(nfo_path):
        return

    # --- Pull the fields we care about ---
    title = data.get("title") or ""
    description = data.get("description") or ""
    upload_date = data.get("upload_date") or ""  # YYYYMMDD
    channel = data.get("channel") or data.get("uploader") or ""
    channel_id = data.get("channel_id") or data.get("uploader_id") or ""
    video_id = data.get("id") or ""
    thumbnail = data.get("thumbnail") or ""
    duration = data.get("duration") or 0

    # --- Trim the description to N paragraphs ---
    parts = re.split(r"\n\s*\n", description.strip())
    # Drop paragraphs that are mostly URLs or promo (link dumps, hashtags, etc.)
    def is_link_dump(p):
        stripped = p.strip()
        if not stripped:
            return True
        # Count lines that are just a URL
        lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        if not lines:
            return True
        url_lines = sum(1 for ln in lines if ln.startswith("http") or ln.startswith("www."))
        # If half or more of the lines are URLs, it's a link dump
        if url_lines / len(lines) >= 0.5:
            return True
        # Also catch paragraphs where the majority of characters are URLs.
        # Use regex to find URLs anywhere in the paragraph, not just at
        # the start of lines, so "Get X here: https://..." is caught.
        url_chars = sum(len(m) for m in re.findall(r"https?://\S+|www\.\S+", stripped))
        total_chars = len(stripped)
        if total_chars > 0 and url_chars / total_chars >= 0.4:
            return True
        # Paragraph with just a few hashtags
        if stripped.startswith("#") and len(stripped) < 200:
            return True
        # Filter common YouTube outro phrases
        lowered = stripped.lower()
        outro_phrases = [
            "thanks for watching",
            "thanks for watchin",
            "thank you for watching",
            "subscribe",
            "like and subscribe",
            "hit the bell",
            "leave a like",
            "comment below",
            "see you next time",
            "stay tuned",
            "want to support me",
            "want to support the",
            "support me and the channel",
            "support the channel",
            "follow me on",
            "check out my",
            "join my",
        ]
        # Only filter short paragraphs (< 200 chars) that contain an outro phrase.
        # Longer paragraphs might have real content mixed in.
        if len(stripped) < 200:
            for phrase in outro_phrases:
                if phrase in lowered:
                    return True
        # Multi-line paragraph where most lines contain a URL is a link list.
        if len(lines) >= 3:
            url_containing_lines = sum(
                1 for ln in lines
                if "http://" in ln or "https://" in ln or "www." in ln
            )
            if url_containing_lines / len(lines) >= 0.5:
                return True
        return False

    filtered = [p for p in parts if not is_link_dump(p)]
    if len(filtered) > max_paragraphs:
        description = "\n\n".join(filtered[:max_paragraphs])
    elif filtered:
        description = "\n\n".join(filtered)
    else:
        description = ""

    # If the surviving description is still URL-heavy, drop it entirely.
    # This handles sponsored-link-only descriptions where the sponsor
    # pitch is the "content."
    if description:
        total = len(description)
        url_chars = sum(len(m) for m in re.findall(r"https?://\S+", description))
        if total > 0 and url_chars / total > 0.25:
            description = ""
        # Also drop descriptions that contain sponsor signals but no
        # real content — no sentence longer than 20 chars that isn't
        # a promo phrase.
        if description:
            lowered = description.lower()
            sponsor_signals = [
                "use code",
                "at checkout",
                "sponsored by",
                "get x% off",
                "discount code",
                "promo code",
                "buy here",
                "get it here",
                "affiliate",
                "sponsor",
            ]
            if any(sig in lowered for sig in sponsor_signals):
                # Check if there's any "real" content — a sentence with
                # >30 chars that doesn't contain a sponsor signal
                sentences = re.split(r"[.!?]\s", description)
                has_real = False
                for s in sentences:
                    s_low = s.lower()
                    if len(s) > 30 and not any(sig in s_low for sig in sponsor_signals):
                        has_real = True
                        break
                if not has_real:
                    description = ""

    # --- Format the air date as YYYY-MM-DD ---
    aired = ""
    if len(upload_date) == 8:
        aired = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    # --- Season number = upload year ---
    season = upload_date[:4] if len(upload_date) >= 4 else ""

    # --- Escape XML-sensitive characters in text fields ---
    def esc(s):
        return (s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
                 .replace('"', "&quot;"))

    lines = ['<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
             "<episodedetails>"]
    if title:
        lines.append(f"  <title>{esc(title)}</title>")
    if description:
        lines.append(f"  <plot>{esc(description)}</plot>")
    if season:
        lines.append(f"  <season>{season}</season>")
    if aired:
        lines.append(f"  <aired>{aired}</aired>")
    if channel:
        lines.append(f"  <studio>{esc(channel)}</studio>")
    if video_id:
        lines.append(f"  <uniqueid type=\"youtube\">{video_id}</uniqueid>")
    if duration:
        lines.append(f"  <runtime>{int(duration) // 60}</runtime>")
    if thumbnail:
        lines.append(f"  <thumb>{esc(thumbnail)}</thumb>")
    lines.append("</episodedetails>")

    try:
        with open(nfo_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"[nfo] wrote episode nfo: {os.path.basename(nfo_path)}", flush=True)
    except OSError as e:
        print(f"[nfo] could not write {nfo_path}: {e}", flush=True)


def write_all_episode_nfos(directory, max_paragraphs=2):
    """Walk a directory and write NFOs for every .info.json found."""
    count = 0
    for root, _, files in os.walk(directory):
        for name in files:
            if name.endswith(".info.json"):
                write_episode_nfo_from_json(
                    os.path.join(root, name), max_paragraphs
                )
                count += 1
    return count


def fix_one_off_nfo(staging_dir):
    """Rewrite <season>1</season> to <season>YYYY</season> in one-off NFOs.

    yt-dlp writes season=1 for YouTube one-off videos because that's what
    YouTube reports. Jellyfin reads the NFO and displays it as "Season 1",
    which doesn't match the folder name (Season YYYY). We rewrite it using
    the date embedded in the filename.
    """
    import re
    for root, _, files in os.walk(staging_dir):
        for name in files:
            if not name.endswith(".nfo"):
                continue
            m = re.search(r"- (\d{4})\.\d{2}\.\d{2} -", name)
            if not m:
                continue
            year = m.group(1)
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                new_content = re.sub(
                    r"<season>\d+</season>",
                    f"<season>{year}</season>",
                    content,
                )
                if new_content != content:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    print(f"[nfo] fixed season -> {year} in {name}", flush=True)
            except OSError as e:
                print(f"[nfo] could not rewrite {path}: {e}", flush=True)


def trim_nfo_plot(nfo_path, max_paragraphs=2):
    """
    Trim the <plot> text inside an NFO to the first N paragraphs.
    YouTube descriptions are often huge (links, socials, credits).
    Keeping just the intro paragraphs gives Jellyfin a clean synopsis.
    """
    try:
        with open(nfo_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return

    m = re.search(r"<plot>(.*?)</plot>", content, re.DOTALL)
    if not m:
        return

    plot = m.group(1)
    parts = re.split(r"\n\s*\n", plot.strip())
    if len(parts) <= max_paragraphs:
        return

    trimmed = "\n\n".join(parts[:max_paragraphs])
    new_content = content.replace(m.group(0), f"<plot>{trimmed}</plot>")

    try:
        with open(nfo_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"[nfo] trimmed plot in {os.path.basename(nfo_path)}", flush=True)
    except OSError as e:
        print(f"[nfo] could not trim {nfo_path}: {e}", flush=True)


def trim_all_nfos(directory, max_paragraphs=2):
    """Walk a directory tree and trim every episode .nfo file."""
    for root, _, files in os.walk(directory):
        for name in files:
            if name.endswith(".nfo") and name not in ("tvshow.nfo", "season.nfo"):
                trim_nfo_plot(os.path.join(root, name), max_paragraphs)


def write_channel_metadata(channel_dir, channel_name, channel_id):
    """
    Download the channel avatar as poster.jpg and write a tvshow.nfo
    with the clean channel name. Both are local; nothing expires.
    """
    import subprocess

    nfo_path = os.path.join(channel_dir, "tvshow.nfo")
    poster_path = os.path.join(channel_dir, "poster.jpg")

    if os.path.exists(nfo_path) and os.path.exists(poster_path):
        print(f"[nfo] metadata already present for {channel_name}", flush=True)
        return

    if not os.path.exists(poster_path) and channel_id:
        avatar_url = None
        # Scrape the channel page for the avatar URL.
        # yt-dlp doesn't expose channel avatars in its metadata,
        # so we grab it directly from the HTML.
        for page_url in (
            f"https://www.youtube.com/channel/{channel_id}",
            f"https://www.youtube.com/channel/{channel_id}/videos",
        ):
            try:
                page = subprocess.run(
                    ["curl", "-sL", "--max-time", "20", page_url],
                    capture_output=True, text=True, timeout=25,
                )
                if page.returncode != 0:
                    continue
                m = re.search(
                    r'https://yt3\.googleusercontent\.com/[a-zA-Z0-9=_-]+',
                    page.stdout,
                )
                if m:
                    avatar_url = m.group(0)
                    break
            except Exception as e:
                print(f"[nfo] scrape error for {channel_name}: {e}", flush=True)

        if avatar_url:
            try:
                dl = subprocess.run(
                    ["curl", "-sL", "--max-time", "30", "-o", poster_path, avatar_url],
                    capture_output=True, timeout=45,
                )
                if dl.returncode == 0 and os.path.exists(poster_path) and os.path.getsize(poster_path) > 0:
                    print(f"[nfo] downloaded avatar for {channel_name}", flush=True)
                else:
                    print(f"[nfo] avatar download failed for {channel_name}", flush=True)
                    if os.path.exists(poster_path) and os.path.getsize(poster_path) == 0:
                        os.remove(poster_path)
            except Exception as e:
                print(f"[nfo] avatar download error for {channel_name}: {e}", flush=True)
        else:
            print(f"[nfo] no avatar URL found for {channel_name}", flush=True)

    if not os.path.exists(nfo_path):
        nfo = [
            '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
            "<tvshow>",
            f"  <title>{channel_name}</title>",
            "</tvshow>",
        ]
        try:
            with open(nfo_path, "w", encoding="utf-8") as f:
                f.write("\n".join(nfo) + "\n")
            print(f"[nfo] wrote {nfo_path}", flush=True)
        except OSError as e:
            print(f"[nfo] could not write {nfo_path}: {e}", flush=True)


def run_download(user_id, url, custom_name=None, cutoff_date=None):
    print(f"[download] queued: {url} for user {user_id}", flush=True)
    with _download_lock:
        _run_download_locked(user_id, url, custom_name, cutoff_date)


def _run_download_locked(user_id, url, custom_name=None, cutoff_date=None):
    ensure_ytdlp_updated()

    # Find the source row this download belongs to, so its files can be
    # tagged with it and governed by that source's retention later.
    try:
        with db() as _c:
            _sr = _c.execute(
                "SELECT id FROM sources WHERE user_id=? AND url=? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, url),
            ).fetchone()
        source_id = _sr["id"] if _sr else None
    except Exception as _e:
        print(f"[retention] source lookup failed: {_e}", flush=True)
        source_id = None
    cmd, staging_dir = build_ytdlp_cmd(user_id, url, custom_name, cutoff_date)
    print(f"[download] starting {url} for user {user_id}", flush=True)

    # Create a task row so the dashboard can show progress
    try:
        purge_old_tasks(user_id, age_seconds=600)
    except Exception:
        pass
    is_channel = not (
        ("youtube.com/watch" in url or "youtu.be/" in url)
        and "/playlist" not in url
    )
    kind = "channel" if is_channel else "video"
    task_id = create_task(user_id, url, kind)

    item_re = re.compile(r"\[download\]\s+Downloading item (\d+) of (\d+)")
    dest_re = re.compile(r"\[download\]\s+Destination:")
    already_re = re.compile(r"already been recorded in the archive")
    pct_re = re.compile(r"\[download\]\s+([\d.]+)%\s+of\s+~?\s*[\d.]+\w+")
    eta_re = re.compile(r"ETA\s+([\d:]+)")

    downloaded = 0

    success = False
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            print(f"[yt-dlp] {line.rstrip()}", flush=True)
            stripped = line.rstrip()

            m = item_re.search(stripped)
            if m:
                cur, total = int(m.group(1)), int(m.group(2))
                update_task(task_id,
                            status="scanning",
                            current_item=cur,
                            total_items=total,
                            message=f"Scanning {cur} of {total}")
                continue

            if dest_re.search(stripped):
                downloaded += 1
                update_task(task_id,
                            status="downloading",
                            downloaded_count=downloaded,
                            progress_pct=0,
                            eta_seconds=None,
                            message=f"Downloading #{downloaded}")
                continue

            if already_re.search(stripped):
                update_task(task_id, message="Skipped (already have it)")
                continue

            m = pct_re.search(stripped)
            if m:
                pct = float(m.group(1))
                eta_s = None
                em = eta_re.search(stripped)
                if em:
                    parts = [int(x) for x in em.group(1).split(":") if x.isdigit()]
                    if len(parts) == 2:
                        eta_s = parts[0] * 60 + parts[1]
                    elif len(parts) == 3:
                        eta_s = parts[0] * 3600 + parts[1] * 60 + parts[2]
                update_task(task_id,
                            progress_pct=pct,
                            eta_seconds=eta_s)
                continue

        proc.wait(timeout=7200)
        print(f"[download] exit code {proc.returncode}", flush=True)
        if proc.returncode in (0, 1, 101):
            success = True
    except Exception as e:
        print(f"[download] error: {e}", flush=True)

    # Count real media files in staging — the true number of videos
    # that will land in the library. yt-dlp's Destination lines include
    # thumbnails, separate video/audio streams, and other non-video files.
    real_videos = 0
    if success:
        media_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v",
                      ".mp3", ".m4a", ".flac", ".opus", ".ogg"}
        for walk_root, _, walk_files in os.walk(staging_dir):
            for n in walk_files:
                if os.path.splitext(n)[1].lower() in media_exts:
                    real_videos += 1

    if success:
        if real_videos == 0:
            # Nothing downloaded — remove the task entirely so the user
            # doesn't see a stale "no-op" entry every scan cycle.
            try:
                with db() as conn:
                    conn.execute("DELETE FROM download_tasks WHERE id=?",
                                 (task_id,))
            except Exception:
                pass
        else:
            finish_task(task_id, "complete",
                        f"Done. {real_videos} video(s) will appear on Jellyfin shortly.")
    else:
        finish_task(task_id, "failed",
                    "Download failed. Check the admin logs for details.")

    with db() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    username = safe_username(row["username"]) if row and row["username"] else user_id
    final_root = f"{MEDIA_ROOT}/{username}/shows"

    if success:
        os.makedirs(final_root, exist_ok=True)

        # Gather the media file paths in staging BEFORE the move, so we
        # can write a retention marker next to each one after it lands.
        _media_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v",
                       ".mp3", ".m4a", ".flac", ".opus", ".ogg"}
        _staged_paths = []
        for _wr, _, _wf in os.walk(staging_dir):
            for _n in _wf:
                if os.path.splitext(_n)[1].lower() in _media_exts:
                    _src_path = os.path.join(_wr, _n)
                    _rel = os.path.relpath(_src_path, staging_dir)
                    _staged_paths.append(os.path.join(final_root, _rel))
        try:
            write_all_episode_nfos(staging_dir)
            fix_one_off_nfo(staging_dir)
            _merge_move(staging_dir, final_root)
            print(f"[download] moved {staging_dir} -> {final_root}", flush=True)
        except Exception as e:
            print(f"[download] move error: {e}", flush=True)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

        # Tag each freshly-moved file with the source it came from, so
        # cleanup can apply this source's retention to it later, and so
        # the Jellyfin webhook can prove the file belongs to ytfinall
        # before deleting it. Every ytfinall download gets a marker,
        # even if the source lookup failed.
        import json as _json
        for _dst in _staged_paths:
            if not os.path.exists(_dst):
                continue
            _marker = _dst + ".ytfinall.json"
            try:
                with open(_marker, "w") as _mf:
                    _json.dump({"source_id": source_id}, _mf)
            except OSError as _me:
                print(f"[retention] marker write failed for "
                      f"{_dst}: {_me}", flush=True)

        # Trim long YouTube descriptions to a short synopsis
        try:
            trim_all_nfos(final_root, max_paragraphs=2)
        except Exception as e:
            print(f"[nfo] trim error: {e}", flush=True)

        # Ensure channel metadata (poster.jpg + tvshow.nfo) exists
        try:
            for entry in os.listdir(final_root):
                entry_path = os.path.join(final_root, entry)
                if not os.path.isdir(entry_path) or entry == "One-Off Videos":
                    continue
                m = re.match(r"^(.+?)\s*\[([A-Za-z0-9_-]+)\]$", entry)
                if not m:
                    continue
                write_channel_metadata(entry_path, m.group(1).strip(), m.group(2))
        except Exception as e:
            print(f"[nfo] channel metadata error: {e}", flush=True)

        # Remove empty season folders left behind by yt-dlp's NFO generation
        cleanup_empty_folders(final_root)
    else:
        print(f"[download] failed, staging kept at {staging_dir}", flush=True)

    with db() as conn:
        row = conn.execute(
            "SELECT library_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    if row and row["library_id"]:
        refresh_jellyfin_library(row["library_id"])


def _has_media_file(folder):
    """Return True if folder contains any video/audio file (recursively)."""
    media_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v",
                  ".mp3", ".m4a", ".flac", ".opus", ".ogg"}
    for _, _, files in os.walk(folder):
        for name in files:
            if os.path.splitext(name)[1].lower() in media_exts:
                return True
    return False


def cleanup_empty_folders(user_root):
    """Remove folders that contain no media files (nfo/json don't count).

    Only removes folders that have been empty for at least 24 hours, to
    avoid deleting folders during the brief window when a file is being
    moved or renamed.
    """
    if not os.path.isdir(user_root):
        return
    now = time.time()
    one_day = 86400
    for root, dirs, _ in os.walk(user_root, topdown=False):
        for d in dirs:
            path = os.path.join(root, d)
            if _has_media_file(path):
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if now - mtime < one_day:
                print(f"[cleanup] skipping recent folder: {path}", flush=True)
                continue
            try:
                shutil.rmtree(path)
                print(f"[cleanup] removed empty folder: {path}", flush=True)
            except OSError as e:
                print(f"[cleanup] could not remove {path}: {e}", flush=True)


def cleanup_user_media(user_id, fallback_retention_days):
    """Delete files past their retention window.

    Each video carries a sidecar marker (.ytfinall.json) recording the
    source that produced it. Cleanup looks up that source's CURRENT
    retention and applies it to the file. Files with no marker (or whose
    source was deleted) fall back to the user's minimum source retention,
    or the admin cap if the user has no sources left.
    """
    import json as _json

    cap = max_retention_days()

    with db() as conn:
        source_rows = conn.execute(
            "SELECT id, retention_days FROM sources WHERE user_id=?",
            (user_id,),
        ).fetchall()
    source_retention = {
        row["id"]: min(max(1, int(row["retention_days"] or cap)), cap)
        for row in source_rows
    }
    if source_retention:
        fallback = min(source_retention.values())
    else:
        fallback = min(max(1, int(fallback_retention_days or cap)), cap)
    now = time.time()
    marker_suffix = ".ytfinall.json"
    with db() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    username = safe_username(row["username"]) if row and row["username"] else user_id
    user_root = f"{MEDIA_ROOT}/{username}"
    if not os.path.isdir(user_root):
        return
    for root, _, files in os.walk(user_root):
        for name in files:
            path = os.path.join(root, name)
            try:
                _mp = path + marker_suffix
                _retention = fallback
                try:
                    with open(_mp) as _mf:
                        _d = _json.load(_mf)
                    _sid = _d.get("source_id")
                    if _sid is not None and _sid in source_retention:
                        _retention = source_retention[_sid]
                except (OSError, ValueError):
                    pass
                if os.path.getmtime(path) < (now - _retention * 86400):
                    os.remove(path)
                    try:
                        os.remove(_mp)
                    except OSError:
                        pass
            except OSError:
                pass

    # Prune folders that no longer contain any media files.
    cleanup_empty_folders(user_root)

    # Ask Jellyfin to rescan so removed items disappear from the UI.
    with db() as conn:
        row = conn.execute(
            "SELECT library_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    if row and row["library_id"]:
        refresh_jellyfin_library(row["library_id"])


# ------------------------------------------------------------------
# Background scheduler
# ------------------------------------------------------------------
def scheduler_loop():
    last_index = 0.0
    last_cleanup = 0.0
    while True:
        now = time.time()

        if now - last_index > index_interval_hours() * 3600 and is_configured():
            try:
                with db() as conn:
                    rows = conn.execute("SELECT * FROM sources").fetchall()
                for row in rows:
                    enqueue_download(row["user_id"], row["url"],
                                     row["name"], row["cutoff"])
            except Exception as e:
                print(f"[scheduler] index error: {e}")
            last_index = now

        if now - last_cleanup > cleanup_interval_hours() * 3600:
            try:
                with db() as conn:
                    rows = conn.execute(
                        "SELECT user_id FROM users"
                    ).fetchall()
                # retention_days isn't a users column — it lives on
                # sources. cleanup_user_media reads each source's own
                # retention from the DB; the second arg is only a
                # fallback for users with no sources left.
                fallback = max_retention_days()
                for row in rows:
                    cleanup_user_media(row["user_id"], fallback)
            except Exception as e:
                print(f"[scheduler] cleanup error: {e}")
            last_cleanup = now

        time.sleep(60)


threading.Thread(target=scheduler_loop, daemon=True).start()


# ------------------------------------------------------------------
# Templates
# ------------------------------------------------------------------
BASE_CSS = """
:root{
  --bg:#f6f8fb;
  --surface:#ffffff;
  --surface-2:#f1f4f8;
  --surface-3:#e9eef4;
  --border:#e3e8ee;
  --border-strong:#cbd3dc;
  --text:#14181d;
  --text-muted:#5b6573;
  --text-faint:#8b95a3;
  --accent:#00a4dc;
  --accent-hover:#008fc2;
  --accent-soft:#e6f6fd;
  --success:#0a8f5a;
  --success-soft:#e3f7ee;
  --danger:#c8384a;
  --danger-soft:#fdecee;
  --shadow-sm:0 1px 2px rgba(15,23,42,.04);
  --shadow:0 1px 3px rgba(15,23,42,.06),0 4px 14px rgba(15,23,42,.05);
  --shadow-lg:0 8px 24px rgba(15,23,42,.08),0 2px 6px rgba(15,23,42,.05);
  --radius:10px;
  --radius-sm:6px;
  --radius-lg:16px;
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Inter","Helvetica Neue",Arial,sans-serif;
  --mono:ui-monospace,"SF Mono",Monaco,"Cascadia Code",Consolas,monospace;
}

@media (prefers-color-scheme: dark){
  :root{
    --bg:#0e1116;
    --surface:#161a21;
    --surface-2:#1c212a;
    --surface-3:#232a35;
    --border:#242b36;
    --border-strong:#39424f;
    --text:#e8ebef;
    --text-muted:#98a1ad;
    --text-faint:#6a7382;
    --accent:#4cb8e6;
    --accent-hover:#6cc8ef;
    --accent-soft:#0e2d3f;
    --success:#34d399;
    --success-soft:#0d2c22;
    --danger:#f47b8a;
    --danger-soft:#3a181e;
    --shadow-sm:0 1px 2px rgba(0,0,0,.35);
    --shadow:0 1px 3px rgba(0,0,0,.4),0 4px 14px rgba(0,0,0,.3);
    --shadow-lg:0 8px 24px rgba(0,0,0,.5),0 2px 6px rgba(0,0,0,.35);
  }
}

*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  font-family:var(--font);
  background:var(--bg);
  color:var(--text);
  max-width:820px;
  margin:0 auto;
  padding:28px 20px 60px;
  line-height:1.55;
  font-size:15.5px;
  -webkit-font-smoothing:antialiased;
  -moz-osx-font-smoothing:grayscale;
}

h2{font-size:1.55rem;font-weight:650;letter-spacing:-.015em;margin:0 0 .35em}
h3{font-size:1.05rem;font-weight:650;letter-spacing:-.01em;margin:1.8em 0 .5em}
p{margin:.5em 0}

label{
  display:block;
  font-weight:600;
  font-size:.88rem;
  color:var(--text);
  margin:0 0 6px;
  letter-spacing:-.005em;
}

input,select,textarea{
  width:100%;
  padding:11px 13px;
  margin:0 0 4px;
  font-size:15px;
  font-family:inherit;
  color:var(--text);
  background:var(--surface);
  border:1px solid var(--border-strong);
  border-radius:var(--radius-sm);
  transition:border-color .15s ease,box-shadow .15s ease,background .15s ease;
  appearance:none;
}
input:focus,select:focus,textarea:focus{
  outline:none;
  border-color:var(--accent);
  box-shadow:0 0 0 3px var(--accent-soft);
}
input::placeholder{color:var(--text-faint)}
input:disabled{background:var(--surface-2);color:var(--text-muted);cursor:not-allowed}

select{
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%238b95a3' d='M6 8 0 0h12z'/%3E%3C/svg%3E");
  background-repeat:no-repeat;
  background-position:right 14px center;
  padding-right:36px;
}

button{
  padding:11px 20px;
  background:var(--accent);
  color:#fff;
  border:none;
  border-radius:var(--radius-sm);
  cursor:pointer;
  font-size:14.5px;
  font-weight:600;
  font-family:inherit;
  letter-spacing:-.005em;
  transition:background .15s ease,transform .06s ease,box-shadow .15s ease;
  box-shadow:var(--shadow-sm);
}
button:hover{background:var(--accent-hover)}
button:active{transform:translateY(1px)}
button:focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
button:disabled{opacity:.55;cursor:not-allowed}

a{color:var(--accent);text-decoration:none;font-weight:500}
a:hover{text-decoration:underline}

.small{color:var(--text-muted);font-size:.86rem}
.error{
  color:var(--danger);
  background:var(--danger-soft);
  padding:11px 14px;
  border-radius:var(--radius-sm);
  border:1px solid color-mix(in srgb,var(--danger) 25%,transparent);
  font-size:.9rem;
}
.ok{
  color:var(--success);
  background:var(--success-soft);
  padding:11px 14px;
  border-radius:var(--radius-sm);
  border:1px solid color-mix(in srgb,var(--success) 25%,transparent);
  font-size:.9rem;
}

.header{
  display:flex;
  justify-content:space-between;
  align-items:center;
  flex-wrap:wrap;
  gap:12px;
  padding-bottom:18px;
  margin-bottom:22px;
  border-bottom:1px solid var(--border);
}

.card{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:18px 22px;
  margin:18px 0;
  box-shadow:var(--shadow-sm);
}

table{
  width:100%;
  border-collapse:separate;
  border-spacing:0;
  margin-top:14px;
  font-size:.92rem;
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  overflow:hidden;
}
th{
  padding:11px 14px;
  text-align:left;
  font-weight:600;
  font-size:.78rem;
  text-transform:uppercase;
  letter-spacing:.05em;
  color:var(--text-muted);
  background:var(--surface-2);
  border-bottom:1px solid var(--border);
}
td{
  padding:12px 14px;
  border-bottom:1px solid var(--border);
  vertical-align:middle;
}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--surface-2)}

.logo{
  display:block;
  max-width:200px;
  margin:12px auto 24px;
  height:auto;
}
.logo-sm{max-width:130px;margin:0;height:auto}

/* Info tooltip pattern */
.field-group{margin-bottom:18px}
.input-row{display:flex;align-items:stretch;gap:8px;margin-top:6px}
.input-row input,.input-row select{margin:0;flex-grow:1}
.info-btn{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  background:transparent;
  color:var(--accent);
  border:none;
  cursor:pointer;
  padding:0 8px;
  font-size:20px;
  line-height:1;
  border-radius:0;
  white-space:nowrap;
  user-select:none;
  transition:color .15s ease,transform .1s ease;
  box-shadow:none;
  flex-shrink:0;
}
.info-btn:hover{color:var(--accent-hover);transform:scale(1.12)}
.info-btn-wide{
  padding:9px 18px;
  border:1px solid var(--border-strong);
  border-radius:var(--radius-sm);
  font-size:13px;
  font-weight:600;
  color:var(--text-muted);
  background:var(--surface-2);
}
.info-btn-wide:hover{
  background:var(--surface-3);
  color:var(--text);
  border-color:var(--border-strong);
  transform:none;
}
.info-toggle{display:none}
.info-toggle:checked ~ .info-box{display:block}
.info-box{
  display:none;
  background:var(--accent-soft);
  border-left:3px solid var(--accent);
  padding:13px 16px;
  margin:10px 0 16px;
  border-radius:0 var(--radius-sm) var(--radius-sm) 0;
  font-size:.88rem;
  color:var(--text);
  line-height:1.55;
}
.info-box ul{margin:6px 0 0;padding-left:20px}
.info-box li{margin-bottom:4px}
.info-box code{
  background:rgba(0,0,0,.06);
  padding:1px 6px;
  border-radius:4px;
  font-family:var(--mono);
  font-size:.85em;
}
@media (prefers-color-scheme: dark){
  .info-box code{background:rgba(255,255,255,.08)}
}

/* Donation footer */
.donate-footer{
  margin-top:48px;
  padding-top:26px;
  border-top:1px solid var(--border);
  text-align:center;
}
.donate-message{
  font-size:.9rem;
  color:var(--text-muted);
  margin-bottom:14px;
  font-style:italic;
}
.donate-title{
  font-size:.75rem;
  color:var(--text-faint);
  text-transform:uppercase;
  letter-spacing:.08em;
  margin-bottom:12px;
  font-weight:600;
}
.donate-links{display:flex;flex-wrap:wrap;gap:10px;justify-content:center}
.donate-link{
  display:inline-block;
  padding:9px 18px;
  background:var(--surface);
  border:1px solid var(--border-strong);
  border-radius:var(--radius-sm);
  color:var(--text);
  font-size:.88rem;
  font-weight:600;
  text-decoration:none;
  transition:background .15s ease,border-color .15s ease,transform .06s ease;
  box-shadow:var(--shadow-sm);
}
.donate-link:hover{
  background:var(--accent-soft);
  border-color:var(--accent);
  color:var(--accent);
  text-decoration:none;
  transform:translateY(-1px);
}

/* Task cards (dashboard) */
.task{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:var(--radius);
  padding:14px 18px;
  margin:10px 0;
  box-shadow:var(--shadow-sm);
}
.task-url{font-size:.78rem;color:var(--text-muted);word-break:break-all;margin-bottom:6px;font-family:var(--mono)}
.task-msg{font-size:.9rem;color:var(--text);margin-bottom:8px;font-weight:500}
.task-bar{background:var(--surface-3);height:8px;border-radius:999px;overflow:hidden}
.task-fill{background:var(--accent);height:100%;width:0%;transition:width .35s ease;border-radius:999px}
.task.complete .task-fill{background:var(--success)}
.task.failed .task-fill{background:var(--danger)}
.task-eta{font-size:.8rem;color:var(--text-muted);margin-top:6px}

/* Mobile */
@media (max-width:600px){
  body{padding:16px 14px 40px;font-size:15px}
  h2{font-size:1.3rem}
  h3{font-size:1rem}
  .logo{max-width:150px;margin:6px auto 18px}
  .logo-sm{max-width:110px}
  .input-row{flex-wrap:wrap}
  .input-row input,.input-row select{width:100%}
  .info-btn{padding:0 6px;font-size:20px}
  .info-btn-wide{width:100%;padding:10px;font-size:13px}
  table{font-size:.85rem}
  th,td{padding:9px 10px}
  .header{flex-direction:column;align-items:flex-start;gap:8px}
  .header>div{width:100%}
  .donate-links{flex-direction:column}
  .donate-link{width:100%;text-align:center}
  .card{padding:14px 16px}
}
"""

LOGIN_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>ytfinall — Login</title><link rel="icon" type="image/png" href="/static/favicon.png">
<style>{{ css }}</style></head><body>
<img src="/static/logo.png" alt="ytfinall" class="logo">
<p>Log in with your Jellyfin account.</p>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
<form method="post" action="/login">
  <label>Username</label>
  <input name="username" required autofocus>
  <label>Password</label>
  <input name="password" type="password" required>
  <button type="submit">Login</button>
</form>
</body></html>
"""

SETUP_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>ytfinall — Setup</title><link rel="icon" type="image/png" href="/static/favicon.png">
<style>{{ css }}</style></head><body>
<img src="/static/logo.png" alt="ytfinall" class="logo">
<h2>Welcome to ytfinall</h2>
<p>This is a one‑time setup. It will be saved inside the app — you won't need to edit any files.</p>

{% if error %}<p class="error">{{ error }}</p>{% endif %}

<style>
/* Self-contained styling for the Admin Setup interface toggles */
.field-group { margin-bottom: 14px; }
.input-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.input-row input { margin: 0 !important; flex-grow: 1; width: 100%; box-sizing: border-box; }
.info-btn { background: transparent; color: #00a4dc; border: none; cursor: pointer; padding: 0 8px; font-size: 20px; line-height: 1; white-space: nowrap; transition: color .15s ease, transform .1s ease; display: inline-flex; align-items: center; justify-content: center; text-align: center; flex-shrink: 0; }
.info-btn:hover { color: #008fc2; transform: scale(1.12); }
.info-toggle { display: none; }
.info-toggle:checked ~ .info-box { display: block; }
.info-box { display: none; background: #f0f4f8; border-left: 4px solid #00a4dc; padding: 12px 16px; margin: 8px 0 16px 0; border-radius: 0 4px 4px 0; font-size: 14px; color: #333; line-height: 1.5; box-sizing: border-box; }
.info-box ul { margin: 6px 0 0 0; padding-left: 20px; }
.info-box li { margin-bottom: 4px; }
</style>

<form method="post" action="/setup">
  <div class="field-group">
    <label>Jellyfin URL (as seen from inside the container)</label>
    <div class="input-row">
      <input name="jellyfin_url" value="{{ jf_url }}" required>
      <label for="setup-url" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="setup-url" class="info-toggle">
    <div class="info-box">
      <strong>Docker Network Matching:</strong>
      <ul>
        <li>If Jellyfin is running on this same host machine, use: <code>http://docker.internal</code></li>
        <li>If running on an external device across your local home network, use that machine's exact network IP: e.g., <code>http://1.xx</code></li>
      </ul>
    </div>
  </div>

  <div class="field-group">
    <label>Jellyfin API key</label>
    <div class="input-row">
      <input name="jellyfin_api_key" placeholder="Paste the key from Jellyfin → Dashboard → API Keys" required>
      <label for="setup-key" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="setup-key" class="info-toggle">
    <div class="info-box">
      <strong>Security & Generation:</strong> Navigate to your Jellyfin administrator panel dashboard, scroll to <em>Advanced → API Keys</em>, generate a dedicated system token named <code>ytfinall</code>, and paste it here. This enables automated library generation.
    </div>
  </div>

  <div class="field-group">
    <label>Maximum download lookback (days)</label>
    <div class="input-row">
      <input name="max_lookback_days" type="number" min="1" max="3650" value="{{ max_lookback }}" required>
      <label for="setup-lookback" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="setup-lookback" class="info-toggle">
    <div class="info-box">
      <strong>Rate Limit Prevention:</strong> Enforces a strict history depth constraint across your end-users. Setting this to a reasonable span (like <code>31</code> days) ensures background automation queries stay fast and crisp, saving your machine from YouTube IP profiling bans.
    </div>
  </div>

  <div class="field-group">
    <label>Maximum retention (days)</label>
    <div class="input-row">
      <input name="max_retention_days" type="number" min="1" max="3650" value="{{ max_retention }}" required>
      <label for="setup-retention" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="setup-retention" class="info-toggle">
    <div class="info-box">
      <strong>Storage Safeguard:</strong> Automatically wipes physical media tracks older than this threshold value on a rolling daily cycle. Helps tightly contain local host system storage pool usage.
    </div>
  </div>

  <button type="submit" style="margin-top: 10px;">Save and continue</button>
</form>
</body></html>
"""

CHANNEL_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>ytfinall — Channel</title><link rel="icon" type="image/png" href="/static/favicon.png">
<style>{{ css }}</style>
<style>
.s-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.s-tile{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
      overflow:hidden;display:flex;flex-direction:column;
      transition:transform .12s ease,box-shadow .12s ease}
.s-tile:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg)}
.s-thumb-wrap{position:relative}
.s-thumb{aspect-ratio:16/9;width:100%;object-fit:cover;background:var(--surface-2);display:block}
.s-body{padding:10px 12px;flex-grow:1;display:flex;flex-direction:column}
.s-title{font-size:.9rem;font-weight:600;line-height:1.35;margin-bottom:4px;
         display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.s-meta{font-size:.78rem;color:var(--text-muted)}
.s-actions{margin-top:auto;padding-top:10px}
.s-actions button{width:100%;padding:8px 12px;font-size:.85rem}
.s-dur{position:absolute;bottom:8px;right:8px;background:rgba(0,0,0,.85);color:#fff;
       font-size:.72rem;font-weight:600;padding:2px 6px;border-radius:3px}
.ch-header{display:flex;gap:16px;align-items:center;padding:18px;
           background:var(--surface);border:1px solid var(--border);
           border-radius:var(--radius);margin-bottom:22px}
.ch-avatar{width:76px;height:76px;border-radius:50%;object-fit:cover;
           background:var(--surface-2);flex-shrink:0}
.ch-info{flex-grow:1;min-width:0}
.ch-name{font-size:1.2rem;font-weight:650;margin:0;line-height:1.25}
.ch-sub{font-size:.85rem;color:var(--text-muted);margin-top:3px}
.ch-actions{display:flex;gap:8px;flex-shrink:0}
.load-wrap{text-align:center;margin:28px 0 0}
.load-more{padding:12px 32px;font-size:.95rem}
@media (max-width:520px){
  .ch-header{flex-wrap:wrap}
  .ch-info{flex-basis:100%}
  .ch-actions{width:100%}
  .ch-actions form,.ch-actions button{width:100%}
}
</style></head><body>

<div class="header">
  <img src="/static/logo.png" alt="ytfinall" class="logo-sm">
  <div>Logged in as <strong>{{ username }}</strong> — <a href="/logout">Logout</a>
  {% if is_admin %} — <a href="/settings">Settings</a>{% endif %} — <a href="/">Dashboard</a> — <a href="/search">Search</a></div>
</div>

<div class="ch-header">
  {% if channel_avatar %}<img class="ch-avatar" src="{{ channel_avatar }}" alt="">{% endif %}
  <div class="ch-info">
    <h2 class="ch-name">{{ channel_title }}</h2>
    <div class="ch-sub">{{ videos|length }} video(s) loaded</div>
  </div>
  <div class="ch-actions">
    <form method="post" action="/add" style="margin:0">
      <input type="hidden" name="url" value="https://www.youtube.com/channel/{{ channel_id }}">
      <input type="hidden" name="retention" value="{{ max_retention }}">
      <button type="submit">Subscribe</button>
    </form>
  </div>
</div>

{% if not videos %}
  <p class="small">No videos found, or the channel could not be reached. Try again in a moment.</p>
{% else %}
  <div class="s-grid">
  {% for r in videos %}
    <div class="s-tile">
      <div class="s-thumb-wrap">
        {% if r.thumbnail %}<img class="s-thumb" src="{{ r.thumbnail }}" alt="" loading="lazy">{% endif %}
        {% if r.duration_human %}<span class="s-dur">{{ r.duration_human }}</span>{% endif %}
      </div>
      <div class="s-body">
        <div class="s-title">{{ r.title }}</div>
        <div class="s-actions">
          <form method="post" action="/add" style="margin:0">
            <input type="hidden" name="url" value="{{ r.url }}">
            <input type="hidden" name="retention" value="{{ max_retention }}">
            <button type="submit">Add video</button>
          </form>
        </div>
      </div>
    </div>
  {% endfor %}
  </div>

  {% if has_more %}
    <div class="load-wrap">
      <a href="/channel/{{ channel_id }}?count={{ count + 30 }}" style="text-decoration:none">
        <button type="button" class="load-more">Load 30 more</button>
      </a>
    </div>
  {% endif %}
{% endif %}

<p class="small" style="margin-top:24px"><a href="/search">← Back to search</a></p>
</body></html>
"""


SEARCH_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>ytfinall — Search</title><link rel="icon" type="image/png" href="/static/favicon.png">
<style>{{ css }}</style>
<style>
.search-bar{display:flex;gap:8px;margin:0 0 14px}
.search-bar input{flex-grow:1;margin:0}
.tabs{display:flex;gap:4px;margin:0 0 18px;border-bottom:1px solid var(--border)}
.tab{padding:8px 14px;font-size:.9rem;font-weight:600;color:var(--text-muted);
     border-bottom:2px solid transparent;margin-bottom:-1px;text-decoration:none}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.tab:hover{text-decoration:none;color:var(--text)}
.s-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px}
.s-tile{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
      overflow:hidden;display:flex;flex-direction:column;
      transition:transform .12s ease,box-shadow .12s ease}
.s-tile:hover{transform:translateY(-2px);box-shadow:var(--shadow-lg)}
.s-thumb-wrap{position:relative}
.s-thumb{aspect-ratio:16/9;width:100%;object-fit:cover;background:var(--surface-2);display:block}
.s-thumb-ch{aspect-ratio:1;width:84px;height:84px;border-radius:50%;
            object-fit:cover;margin:18px auto 10px;display:block;background:var(--surface-2)}
.s-body{padding:10px 12px;flex-grow:1;display:flex;flex-direction:column}
.s-title{font-size:.9rem;font-weight:600;line-height:1.35;margin-bottom:4px;
         display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.s-meta{font-size:.78rem;color:var(--text-muted)}
.s-actions{margin-top:auto;padding-top:10px}
.s-actions button{width:100%;padding:8px 12px;font-size:.85rem}
.s-dur{position:absolute;bottom:8px;right:8px;background:rgba(0,0,0,.85);color:#fff;
       font-size:.72rem;font-weight:600;padding:2px 6px;border-radius:3px}
</style></head><body>

<div class="header">
  <img src="/static/logo.png" alt="ytfinall" class="logo-sm">
  <div>Logged in as <strong>{{ username }}</strong> — <a href="/logout">Logout</a>
  {% if is_admin %} — <a href="/settings">Settings</a>{% endif %} — <a href="/">Dashboard</a></div>
</div>

<h2>Search YouTube</h2>
<form method="get" action="/search" class="search-bar">
  <input name="q" value="{{ q }}" placeholder="Search for a video or channel…" autofocus autocomplete="off">
  <input type="hidden" name="type" value="{{ kind }}">
  <button type="submit">Search</button>
</form>

<div class="tabs">
  <a class="tab {% if kind=='video' %}active{% endif %}" href="/search?q={{ q|urlencode }}&type=video">Videos</a>
  <a class="tab {% if kind=='channel' %}active{% endif %}" href="/search?q={{ q|urlencode }}&type=channel">Channels</a>
</div>

{% if not q %}
  <p class="small">Type a query above. Results come straight from YouTube.</p>
{% elif not results %}
  <p class="small">No {{ kind }} results for <strong>{{ q }}</strong>.</p>
{% else %}
  <div class="s-grid">
  {% for r in results %}
    <div class="s-tile">
      {% if r.kind == 'video' %}
        <div class="s-thumb-wrap">
          {% if r.thumbnail %}<img class="s-thumb" src="{{ r.thumbnail }}" alt="" loading="lazy">{% endif %}
          {% if r.duration_human %}<span class="s-dur">{{ r.duration_human }}</span>{% endif %}
        </div>
        <div class="s-body">
          <div class="s-title">{{ r.title }}</div>
          <div class="s-meta">{{ r.channel }}</div>
          <div class="s-actions">
            <form method="post" action="/add" style="margin:0">
              <input type="hidden" name="url" value="{{ r.url }}">
              <input type="hidden" name="retention" value="{{ max_retention }}">
              <button type="submit">Add video</button>
            </form>
          </div>
        </div>
      {% else %}
        {% if r.thumbnail %}<img class="s-thumb-ch" src="{{ r.thumbnail }}" alt="" loading="lazy">{% endif %}
        <div class="s-body" style="text-align:center">
          <div class="s-title">{{ r.title }}</div>
          <div class="s-meta">{{ r.subscribers }}</div>
          <div class="s-actions" style="display:flex;gap:6px">
            <a href="/channel/{{ r.id }}" style="flex:1;text-decoration:none">
              <button type="button" style="width:100%;background:#6a7382">Videos</button>
            </a>
            <form method="post" action="/add" style="margin:0;flex:1">
              <input type="hidden" name="url" value="{{ r.url }}">
              <input type="hidden" name="retention" value="{{ max_retention }}">
              <button type="submit" style="width:100%">Subscribe</button>
            </form>
          </div>
        </div>
      {% endif %}
    </div>
  {% endfor %}
  </div>

  {% if has_more %}
    <div style="text-align:center;margin:28px 0 0">
      <a href="/search?q={{ q|urlencode }}&type={{ kind }}&count={{ count + 30 }}" style="text-decoration:none">
        <button type="button" style="padding:12px 32px;font-size:.95rem">Load 30 more</button>
      </a>
    </div>
  {% endif %}
{% endif %}

<p class="small" style="margin-top:24px"><a href="/">← Back to dashboard</a></p>
</body></html>
"""


DASHBOARD = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>ytfinall</title><link rel="icon" type="image/png" href="/static/favicon.png">
<style>{{ css }}</style></head><body>
<div class="header">
  <img src="/static/logo.png" alt="ytfinall" class="logo-sm">
  <div>Logged in as <strong>{{ username }}</strong> — <a href="/logout">Logout</a>
  {% if is_admin %} — <a href="/settings">Settings</a>{% endif %}</div>
</div>

<div class="card">
<p class="small">Admin limits: download from the last <strong>{{ max_lookback }}</strong> days,
keep media up to <strong>{{ max_retention }}</strong> days.</p>
</div>

<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
    <div>
      <strong>Auto-delete after watching</strong>
      <p class="small" style="margin:4px 0 0">
        {% if delete_on_finish %}
          <span style="color:var(--success)">● Enabled</span> — videos are removed once you finish watching them in Jellyfin.
        {% else %}
          <span style="color:var(--text-muted)">● Disabled</span> — videos stay until their retention period expires.
        {% endif %}
      </p>
    </div>
    <form method="post" action="/toggle-delete-on-finish" style="margin:0">
      <button type="submit">{% if delete_on_finish %}Disable{% else %}Enable{% endif %}</button>
    </form>
  </div>
</div>

<form method="get" action="/search" style="display:flex;gap:8px;margin:18px 0">
  <input name="q" placeholder="Search YouTube for videos or channels…" style="margin:0;flex-grow:1" autocomplete="off">
  <button type="submit">Search</button>
</form>

<div id="tasks-container" style="display:none;margin:18px 0">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <h3 style="margin:0">Active downloads</h3>
    <button type="button" id="tasks-clear" style="background:#888;padding:6px 12px;font-size:13px">Clear finished</button>
  </div>
  <div id="tasks-list"></div>
</div>



<h3>Add a one-off video</h3>
<form method="post" action="/add">
  <div class="field-group">
    <label>Video URL</label>
    <div class="input-row">
      <input name="url" required placeholder="https://www.youtube.com/watch?v=...">
      <label for="info-oneoff" class="info-btn">ⓘ</label>
    </div>
    <p class="small" style="margin-top:6px">Paste a single video URL. It goes into a shared <strong>One-Off Videos</strong> folder in your library.</p>
    <input type="checkbox" id="info-oneoff" class="info-toggle">
    <div class="info-box">
      <strong>One-off videos:</strong> For individual videos you don't want to subscribe to a whole channel for. Each video is placed in <code>One-Off Videos/Season YYYY/</code> inside your library. Retention works the same as for channels.
    </div>
  </div>


  <div class="field-group">
    <label>Retention period (1–{{ max_retention }} days)</label>
    <div class="input-row">
      <input name="retention" type="number" min="1" max="{{ max_retention }}" value="{{ max_retention }}">
      <label for="info-oneoff-ret" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="info-oneoff-ret" class="info-toggle">
    <div class="info-box">
      <strong>Storage Maintenance Tip:</strong> How long this video stays in your library before automated cleanup deletes it. Unlike channel subscriptions, one-off videos can be any age — the retention timer is the only limit.
    </div>
  </div>

  <button type="submit" style="margin-top: 10px;">Add &amp; Download</button>
</form>

<h3>Add a YouTube channel</h3>
<form method="post" action="/add">
  <div class="field-group">
    <label>Source URL</label>
    <div class="input-row">
      <input name="url" required placeholder="https://youtube.com">
      <label for="info-url" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="info-url" class="info-toggle">
    <div class="info-box">
      <strong>Best Use Tips:</strong>
      <ul>
        <li>Paste the full URL of a channel root (e.g., <code>.../@ChannelName</code>), a specific playlist, or a single video link.</li>
        <li><strong>Pro-Tip:</strong> Channel subscriptions will automatically look at the channel's video catalog safely without hammering shorts or live streams unnecessarily.</li>
      </ul>
    </div>
  </div>

  <div class="field-group">
    <label>Custom Name (optional)</label>
    <div class="input-row">
      <input name="name" placeholder="Leave blank to use the channel's name">
      <label for="info-name" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="info-name" class="info-toggle">
    <div class="info-box">
      <strong>What this does:</strong> Sets the display folder name inside your Jellyfin library. If left blank, the channel's actual YouTube name is used automatically.
    </div>
  </div>

  <div class="field-group">
    <label>Download Cutoff Date (optional)</label>
    <div class="input-row">
      <input name="cutoff" type="date">
      <label for="info-cutoff" class="info-btn">ⓘ</label>
    </div>
    <p class="small" style="margin-top:6px">Leave blank to download the last <strong>7 days</strong>.</p>
    <input type="checkbox" id="info-cutoff" class="info-toggle">
    <div class="info-box">
      <strong>How it works:</strong>
      <ul>
        <li>Limits downloads to items posted <em>after</em> this specific target timestamp.</li>
        <li>If left blank, the default is the last <strong>7 days</strong>.</li>
        <li>The date cannot be earlier than your administrator's limit of <strong>{{ max_lookback }} days</strong>.</li>
      </ul>
    </div>
  </div>

  <div class="field-group">
    <label>Retention period (1–{{ max_retention }} days)</label>
    <div class="input-row">
      <input name="retention" type="number" min="1" max="{{ max_retention }}" value="{{ max_retention }}">
      <label for="info-retention" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="info-retention" class="info-toggle">
    <div class="info-box">
      <strong>Storage Maintenance Tip:</strong> This controls how long downloaded media assets sit inside your active library before automated cache scrubbing deletes them. Your administrator enforces an upper tier threshold of <strong>{{ max_retention }} days</strong>.
    </div>
  </div>

  <button type="submit" style="margin-top: 10px;">Add &amp; Download</button>
</form>

<h3>Your sources</h3>
<div style="overflow-x:auto">
<table>
<tr><th>Added</th><th>URL</th><th>Cutoff</th><th>Retention</th><th></th></tr>
{% for s in sources %}
<tr>
  <td>{{ s.created_at[:10] if s.created_at else "—" }}</td>
  <td>{{ s.url }}</td>
  <td>{{ s.cutoff or "Last 7 days" }}</td>
  <td>{{ s.retention_days }} days</td>
  <td>
    <div style="display:flex;gap:8px;align-items:center">
      <a href="/edit/{{ s.id }}"><button type="button">Edit</button></a>
      <form method="post" action="/delete/{{ s.id }}" style="margin:0">
        <button type="submit">Remove</button>
      </form>
    </div>
  </td>
</tr>
{% endfor %}
</table>
</div>

<script>
(function () {
  const container = document.getElementById('tasks-container');
  const list = document.getElementById('tasks-list');
  if (!container || !list) return;

  function esc(s) {
    return String(s || '').replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
  }

  function fmtTime(sec) {
    if (sec == null) return '';
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + 's';
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return m + 'm ' + s + 's';
  }

  function render(tasks) {
    if (!tasks.length) {
      container.style.display = 'none';
      list.innerHTML = '';
      return;
    }
    container.style.display = 'block';
    list.innerHTML = '';
    for (const t of tasks) {
      const div = document.createElement('div');
      div.className = 'task ' + (t.status || '');
      const pct = (t.status === 'complete') ? 100 : (t.progress_pct || 0);

      let eta = '';
      if (t.status === 'downloading' && t.eta_seconds != null) {
        eta = 'ETA ' + fmtTime(t.eta_seconds) + ' remaining';
      } else if (t.status === 'scanning' && t.total_items) {
        eta = 'Checking video ' + t.current_item + ' of ' + t.total_items;
      } else if (t.status === 'complete') {
        eta = t.message || 'Done.';
      } else if (t.status === 'failed') {
        eta = t.message || 'Failed.';
      } else if (t.message) {
        eta = t.message;
      }

      div.innerHTML =
        '<div class="task-url">' + esc(t.url) + '</div>' +
        '<div class="task-msg">' + esc(t.message || t.status || '') + '</div>' +
        '<div class="task-bar"><div class="task-fill" style="width:' + pct + '%"></div></div>' +
        '<div class="task-eta">' + esc(eta) + '</div>';
      list.appendChild(div);
    }
  }

  let timer = null;
  async function poll() {
    try {
      const r = await fetch('/api/tasks');
      if (!r.ok) return;
      const d = await r.json();
      render(d.tasks || []);
    } catch (_) {}
  }

  function start() {
    if (timer !== null) return;
    poll();
    timer = setInterval(poll, 2000);
  }
  function stop() {
    if (timer === null) return;
    clearInterval(timer);
    timer = null;
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stop(); else start();
  });

  const clearBtn = document.getElementById('tasks-clear');
  if (clearBtn) {
    clearBtn.addEventListener('click', async () => {
      clearBtn.disabled = true;
      try {
        await fetch('/api/tasks/clear', { method: 'POST' });
      } catch (_) {}
      clearBtn.disabled = false;
      poll();
    });
  }

  if (!document.hidden) start();
})();
</script>

<div class="card" style="margin-top:32px">
  <h3 style="margin-top:0">Browser Extension</h3>
  <p class="small">A companion browser extension lets you right-click any YouTube video, channel, or playlist and send it straight to your ytfinall server without copy-pasting.</p>
  <label for="info-extension" class="info-btn" style="display:inline-block;margin-top:10px">Show install instructions</label>
  <input type="checkbox" id="info-extension" class="info-toggle">
  <div class="info-box" style="margin-top:10px">
    <p style="margin-top:0"><strong>Install:</strong></p>
    <ul>
      <li><a href="https://microsoftedge.microsoft.com/addons/detail/ytfinall-connector/hdfpngaekagheekoajkjhdjoomppmlhj" target="_blank" rel="noopener noreferrer">Microsoft Edge Add-ons — ytfinall Connector</a></li>
      <li>Firefox Add-ons (AMO) — pending review</li>
      <li>Chrome — <a href="https://github.com/jnracreates/ytfinall/blob/main/INSTALL_CHROME.md" target="_blank" rel="noopener noreferrer">manual install required</a> (Chrome Web Store policy)</li>
    </ul>
    <p><strong>Works on:</strong> Microsoft Edge (desktop and mobile), Firefox (desktop and Android — pending AMO approval), any Chromium browser that can install from the Edge Add-ons store, and Chrome via the manual install guide.</p>
    <p><strong>Mobile note:</strong> Right-click menus don't exist on mobile browsers. On mobile, tap the extension icon and paste the YouTube URL into the popup instead.</p>
    <p style="margin-bottom:6px"><strong>How it works:</strong></p>
    <ol style="margin:0">
      <li>Click the extension icon and log in with your Jellyfin credentials</li>
      <li>Right-click any YouTube link (or paste a URL on mobile)</li>
      <li>The URL is sent to your ytfinall server and queued for download</li>
      <li>The video appears in your Jellyfin library when the download completes</li>
    </ol>
  </div>
</div>

<div class="donate-footer">
  <div class="donate-message">{{ donation_message }}</div>
  <div class="donate-title">Support this project</div>
  <div class="donate-links">
    {% for label, url in donation_links %}
      <a href="{{ url }}" target="_blank" rel="noopener noreferrer" class="donate-link">{{ label }}</a>
    {% endfor %}
  </div>
</div>

</body></html>
"""

SETTINGS_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>ytfinall — Settings</title><link rel="icon" type="image/png" href="/static/favicon.png">
<style>{{ css }}</style></head><body>
<img src="/static/logo.png" alt="ytfinall" class="logo">
<h2>Admin settings</h2>
{% if error %}<p class="error">{{ error }}</p>{% endif %}
{% if ok %}<p class="ok">{{ ok }}</p>{% endif %}

{% if locked %}
  <p>Enter the current Jellyfin API key to unlock settings.</p>
  <form method="post" action="/settings">
    <input name="unlock_key" placeholder="Current API key" required>
    <button type="submit">Unlock</button>
  </form>
{% else %}
  {% if not session.get('settings_unlocked') %}
    <p class="error">Settings must be unlocked to edit.</p>
  {% endif %}
  <form method="post" action="/settings">
    <div class="field-group">
      <label>Jellyfin URL</label>
      <div class="input-row">
        <input name="jellyfin_url" value="{{ jf_url }}" required>
        <label for="settings-url" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-url" class="info-toggle">
      <div class="info-box">
        <strong>Docker Network Matching:</strong>
        <ul>
          <li>If Jellyfin is running on this same host machine, use: <code>http://docker.internal</code></li>
          <li>If running on an external device across your local home network, use that machine's exact network IP: e.g., <code>http://1.xx</code></li>
        </ul>
      </div>
    </div>

    <div class="field-group">
      <label>Jellyfin API key</label>
      <div class="input-row">
        <input name="jellyfin_api_key" value="{{ api_key }}" required>
        <label for="settings-key" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-key" class="info-toggle">
      <div class="info-box">
        <strong>Security & Generation:</strong> Navigate to your Jellyfin administrator panel dashboard, scroll to <em>Advanced → API Keys</em>, generate a dedicated system token named <code>ytfinall</code>, and paste it here. This enables automated library generation.
      </div>
    </div>

    <div class="field-group">
      <label>Maximum download lookback (days)</label>
      <div class="input-row">
        <input name="max_lookback_days" type="number" min="1" max="3650" value="{{ max_lookback }}" required>
        <label for="settings-lookback" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-lookback" class="info-toggle">
      <div class="info-box">
        <strong>Rate Limit Prevention:</strong> Enforces a strict history depth constraint across your end-users. Setting this to a reasonable span (like <code>31</code> days) ensures background automation queries stay fast and crisp, saving your machine from YouTube IP profiling bans.
      </div>
    </div>

    <div class="field-group">
      <label>Maximum retention (days)</label>
      <div class="input-row">
        <input name="max_retention_days" type="number" min="1" max="3650" value="{{ max_retention }}" required>
        <label for="settings-retention" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-retention" class="info-toggle">
      <div class="info-box">
        <strong>Storage Safeguard:</strong> Automatically wipes physical media tracks older than this threshold value on a rolling daily cycle. Helps tightly contain local host system storage pool usage.
      </div>
    </div>

    <h3 style="margin-top:32px;border-top:1px solid #ddd;padding-top:18px">Advanced</h3>
    <p class="small">Change these only if you know what you're doing. Bad values can break downloads.</p>

    <div class="field-group">
      <label>Playlist scan limit (items per channel)</label>
      <div class="input-row">
        <input name="playlist_end" type="number" min="1" max="500" value="{{ playlist_end }}" required>
        <label for="settings-playlist" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-playlist" class="info-toggle">
      <div class="info-box">
        <strong>Speed vs. Coverage:</strong>
        <ul>
          <li>yt-dlp looks at the newest N items per channel each scan. Lower = faster scans but may miss older videos inside your lookback window.</li>
          <li>Default <code>35</code> covers roughly 1 month of uploads for a channel that posts 1–2 videos per day.</li>
        </ul>
      </div>
    </div>

    <div class="field-group">
      <label>Sleep between requests (seconds)</label>
      <div class="input-row">
        <input name="sleep_requests" type="number" min="0" max="60" value="{{ sleep_requests }}" required>
        <label for="settings-sleepreq" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-sleepreq" class="info-toggle">
      <div class="info-box">
        <strong>Rate-limit Defense:</strong> Pause applied between every HTTP call yt-dlp makes for a single video. Higher values look more like a human browser and reduce the chance YouTube throttles your IP. Set to <code>0</code> only if you're sure you won't be rate limited.
      </div>
    </div>

    <div class="field-group">
      <label>Minimum sleep between videos (seconds)</label>
      <div class="input-row">
        <input name="sleep_interval" type="number" min="0" max="600" value="{{ sleep_interval }}" required>
        <label for="settings-sleepmin" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-sleepmin" class="info-toggle">
      <div class="info-box">
        <strong>Per-video pause:</strong> The shortest wait yt-dlp will take before starting the next video. Raise this if you see "rate-limited" messages during a scan.
      </div>
    </div>

    <div class="field-group">
      <label>Maximum sleep between videos (seconds)</label>
      <div class="input-row">
        <input name="max_sleep_interval" type="number" min="0" max="600" value="{{ max_sleep_interval }}" required>
        <label for="settings-sleepmax" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-sleepmax" class="info-toggle">
      <div class="info-box">
        <strong>Randomized upper bound:</strong> yt-dlp picks a random pause between the minimum and maximum. Randomness makes the traffic pattern look less bot-like.
      </div>
    </div>

    <div class="field-group">
      <label>Maximum resolution (vertical pixels)</label>
      <div class="input-row">
        <input name="max_resolution" type="number" min="144" max="4320" value="{{ max_resolution }}" required>
        <label for="settings-res" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-res" class="info-toggle">
      <div class="info-box">
        <strong>Quality cap:</strong> Highest video height to download. yt-dlp picks the closest available if the exact height isn't offered. Common values: <code>1080</code>, <code>1440</code>, <code>2160</code>.
      </div>
    </div>

    <div class="field-group">
      <label>Media container</label>
      <div class="input-row">
        <input name="media_container" value="{{ media_container }}" required>
        <label for="settings-container" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-container" class="info-toggle">
      <div class="info-box">
        <strong>Output format:</strong> The file extension yt-dlp merges video+audio into. <code>mp4</code> is the safest for Jellyfin. <code>mkv</code> handles more exotic audio codecs but may transcode in some players.
      </div>
    </div>

    <div class="field-group">
      <label>Output template (yt-dlp <code>-o</code> syntax)</label>
      <div class="input-row">
        <input name="outtmpl" value="{{ outtmpl }}" required>
        <label for="settings-outtmpl" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-outtmpl" class="info-toggle">
      <div class="info-box">
        <strong>File naming pattern:</strong>
        <ul>
          <li>Uses yt-dlp's <code>%(field)s</code> placeholders, not Jinja <code>&#123;&#123; &#125;&#125;</code>.</li>
          <li>Common fields: <code>%(channel)s</code>, <code>%(channel_id)s</code>, <code>%(upload_date&gt;%Y)s</code>, <code>%(title)s</code>, <code>%(id)s</code>, <code>%(ext)s</code>.</li>
          <li>This is relative to the staging folder and should keep the <code>Channel [id]/Season YYYY/</code> structure for Jellyfin.</li>
        </ul>
      </div>
    </div>

    <div class="field-group">
      <label>Extra yt-dlp arguments (space-separated)</label>
      <div class="input-row">
        <input name="extra_ytdlp_args" value="{{ extra_ytdlp_args }}" placeholder="e.g. --geo-bypass --no-check-certificates">
        <label for="settings-extra" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-extra" class="info-toggle">
      <div class="info-box">
        <strong>Escape hatch:</strong> Anything you'd type on the yt-dlp command line goes here. Appended at the end, so they can override earlier flags. Examples: <code>--no-break-on-existing</code>, <code>--proxy socks5://host:1080</code>.
      </div>
    </div>

    <div class="field-group">
      <label>Index interval (hours)</label>
      <div class="input-row">
        <input name="index_interval_hours" type="number" min="1" max="720" value="{{ index_interval_hours }}" required>
        <label for="settings-index" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-index" class="info-toggle">
      <div class="info-box">
        <strong>Channel rescan frequency:</strong> How often every subscribed channel is re-checked for new uploads. <code>12</code> is a good balance between freshness and YouTube rate limits.
      </div>
    </div>

    <div class="field-group">
      <label>Cleanup interval (hours)</label>
      <div class="input-row">
        <input name="cleanup_interval_hours" type="number" min="1" max="720" value="{{ cleanup_interval_hours }}" required>
        <label for="settings-cleanup" class="info-btn">ⓘ</label>
      </div>
      <input type="checkbox" id="settings-cleanup" class="info-toggle">
      <div class="info-box">
        <strong>Retention sweep frequency:</strong> How often old files are deleted based on each source's retention period. <code>24</code> means media expires within a day of its retention deadline.
      </div>
    </div>

    <button type="submit" style="margin-top: 10px;">Save</button>
  </form>
{% endif %}

<div class="field-group" style="margin-top:24px;border-top:1px solid #ddd;padding-top:18px">
  <label>User maintenance</label>
  <p class="small">Clear a user's download archive to force yt-dlp to re-download their library. Useful after data loss or for a full rebuild.</p>

  {% for u in user_list %}
  <div style="background:#f7f9fb;border:1px solid #e1e6eb;border-radius:6px;padding:12px 16px;margin:10px 0">
    <strong>{{ u.username }}</strong>
    <span class="small" style="margin-left:8px">
      — {{ u.sources }} sources, {{ u.archive_count }} archived videos
    </span>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button type="button" class="admin-archive-btn" data-uid="{{ u.user_id }}" data-action="clear"
              style="background:#c33;padding:8px 14px;font-size:14px">
        Clear Archive
      </button>
      <button type="button" class="admin-archive-btn" data-uid="{{ u.user_id }}" data-action="clear-retrigger"
              style="background:#00a4dc;padding:8px 14px;font-size:14px">
        Clear + Rescan
      </button>
      <button type="button" class="admin-archive-btn" data-uid="{{ u.user_id }}" data-action="retrigger"
              style="background:#888;padding:8px 14px;font-size:14px">
        Just Rescan
      </button>
    </div>
    <div class="admin-archive-status" data-uid="{{ u.user_id }}" style="margin-top:8px;font-size:13px"></div>
  </div>
  {% endfor %}
</div>

<div class="field-group" style="margin-top:24px;border-top:1px solid #ddd;padding-top:18px">
  <label>User storage</label>
  <p class="small">Video count and disk usage per user. Computed on demand — may take a moment for large libraries.</p>
  <div id="user-stats-loading" class="small">Loading stats…</div>
  <div id="user-stats-box" style="display:none">
    <table id="user-stats-table" style="width:100%;border-collapse:collapse;margin-top:8px">
      <thead>
        <tr style="border-bottom:1px solid #ddd">
          <th style="text-align:left;padding:8px">User</th>
          <th style="text-align:right;padding:8px">Videos</th>
          <th style="text-align:right;padding:8px">Folders</th>
          <th style="text-align:right;padding:8px">Storage</th>
        </tr>
      </thead>
      <tbody id="user-stats-body"></tbody>
      <tfoot id="user-stats-foot"></tfoot>
    </table>
    <button type="button" id="user-stats-refresh" style="background:#888;padding:6px 12px;font-size:13px;margin-top:10px">Refresh</button>
  </div>
</div>

<div class="field-group" style="margin-top:24px;border-top:1px solid #ddd;padding-top:18px">
  <label>Jellyfin Webhook — auto-delete after watching</label>
  <p class="small">Users can toggle auto-delete on their dashboard. For it to fire, Jellyfin must be configured to POST to this endpoint. In Jellyfin, install the <strong>Webhook</strong> plugin, add a <strong>Generic Destination</strong>, and paste this URL:</p>
  <div style="background:#111;color:#0f0;font-family:'SF Mono',Monaco,Consolas,monospace;font-size:12px;padding:12px;border-radius:6px;word-break:break-all;margin-top:6px">
    http://YOUR-SERVER-IP:6842/jellyfin/webhook?token={{ webhook_secret() }}
  </div>
  <p class="small" style="margin-top:10px">
    Replace <code>YOUR-SERVER-IP</code> with the address Jellyfin uses to reach this container (e.g. <code>192.168.1.10</code>, or the container name if on the same Docker network).
  </p>

  <p class="small" style="margin-top:14px"><strong>Configure the destination like this:</strong></p>
  <ul class="small" style="margin:6px 0 0 0;padding-left:20px;line-height:1.7">
    <li><strong>Destination type:</strong> <code>Generic</code> (do <em>not</em> use <code>Generic Form</code> — it always sends form-encoded data and ignores both the template and headers)</li>
    <li><strong>Notification Type:</strong> tick only <strong>Playback Stop</strong></li>
    <li><strong>Item Type:</strong> tick only <strong>Episodes</strong></li>
    <li><strong>User Filter:</strong> leave all users unticked (fires for everyone — ytfinall decides per-user)</li>
    <li><strong>Send All Properties:</strong> leave <strong>UNCHECKED</strong></li>
    <li><strong>Request Header:</strong> add a row with key <code>Content-Type</code> and value <code>application/json</code></li>
    <li><strong>Template:</strong> paste this exactly (the Handlebars variables are populated by the plugin):
      <pre style="background:#f4f6f8;padding:10px;border-radius:4px;font-size:12px;overflow-x:auto;margin:6px 0 0 0">{
  "NotificationType": "{{NotificationType}}",
  "PlayedToCompletion": "{{PlayedToCompletion}}",
  "UserId": "{{UserId}}",
  "UserName": "{{UserName}}",
  "Path": "{{Path}}",
  "Name": "{{Name}}"
}</pre>
    </li>
    <li><strong>After saving:</strong> restart the Jellyfin container — the plugin only reloads destination config on restart</li>
  </ul>

  <p class="small" style="margin-top:14px"><strong>Path &amp; user note:</strong> recent versions of the Webhook plugin leave <code>{{Path}}</code> and <code>{{UserName}}</code> empty in the template payload. ytfinall handles this automatically — it looks up the user from <code>UserId</code> and finds the file by matching the video title in the user's library folder. You should see a working <code>[webhook]</code> line in the Live Logs panel within a second of finishing a video.</p>

  <p class="small">If deletion is refused, check the Live Logs panel for a <code>[webhook] refusing…</code> line — it will say whether the path was outside the media root or the file was missing its <code>.ytfinall.json</code> marker.</p>
</div>

<div class="field-group" style="margin-top:24px;border-top:1px solid #ddd;padding-top:18px">
  <label>Live logs</label>
  <p class="small">Updates every 2 seconds while this tab is visible. Shows the last 500 lines.</p>
  <div id="log-box" style="background:#111;color:#0f0;font-family:'SF Mono',Monaco,Consolas,monospace;font-size:12px;padding:12px;border-radius:6px;height:320px;overflow-y:auto;white-space:pre-wrap;line-height:1.35;word-break:break-all">Loading…</div>
  <div style="margin-top:8px">
    <button type="button" id="log-clear" style="background:#555;padding:6px 12px;font-size:13px">Clear buffer</button>
  </div>
</div>

<div class="field-group" style="margin-top:24px">
  <label>YouTube cookies.txt (optional)</label>
  <p class="small">Used by yt-dlp for age-restricted or members-only content. Upload a fresh export from a browser extension like "Get cookies.txt LOCALLY".</p>

  <div id="drop-zone" style="border: 2px dashed #007bff; border-radius: 8px; padding: 40px; text-align: center; cursor: pointer; background: #f8f9fa; transition: background 0.3s;">
    <p style="margin: 0; font-size: 16px; color: #333;">Drag and drop your <b>cookies.txt</b> file here</p>
    <p style="margin: 5px 0 0 0; font-size: 12px; color: #666;">or click to select file</p>
    <input type="file" id="file-input" accept=".txt" style="display: none;">
  </div>
  <div id="upload-status" style="margin-top: 15px; font-weight: bold; text-align: center;"></div>
</div>

<script>
(function () {
  const dropZone = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');
  const status = document.getElementById('upload-status');

  dropZone.addEventListener('click', () => fileInput.click());

  ['dragenter', 'dragover'].forEach(ev =>
    dropZone.addEventListener(ev, e => {
      e.preventDefault();
      dropZone.style.background = '#e3f2fd';
    })
  );

  ['dragleave', 'drop'].forEach(ev =>
    dropZone.addEventListener(ev, e => {
      e.preventDefault();
      dropZone.style.background = '#f8f9fa';
    })
  );

  dropZone.addEventListener('drop', e => {
    if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files.length) upload(fileInput.files[0]);
  });

  function upload(file) {
    if (!file.name.toLowerCase().endsWith('.txt')) {
      status.style.color = '#c00';
      status.textContent = 'Please upload a .txt file';
      return;
    }
    const fd = new FormData();
    fd.append('cookies', file);
    status.style.color = '#666';
    status.textContent = 'Uploading...';

    fetch('/upload-cookies', { method: 'POST', body: fd })
      .then(r => r.json())
      .then(data => {
        status.style.color = data.ok ? '#0a5' : '#c00';
        status.textContent = data.message;
      })
      .catch(err => {
        status.style.color = '#c00';
        status.textContent = 'Upload failed: ' + err;
      });
  }
})();

// Live log polling — only while the tab is visible
(function () {
  const box = document.getElementById('log-box');
  if (!box) return;
  let pinnedToBottom = true;
  let pollTimer = null;

  box.addEventListener('scroll', () => {
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
    pinnedToBottom = atBottom;
  });

  async function poll() {
    try {
      const r = await fetch('/admin/logs');
      if (!r.ok) return;
      const d = await r.json();
      if (!Array.isArray(d.lines)) return;
      const text = d.lines.join('\\n');
      if (box.dataset.last === text) return;
      box.dataset.last = text;
      box.textContent = text;
      if (pinnedToBottom) {
        box.scrollTop = box.scrollHeight;
      }
    } catch (_) {}
  }

  function startPolling() {
    if (pollTimer !== null) return;
    poll();
    pollTimer = setInterval(poll, 2000);
  }

  function stopPolling() {
    if (pollTimer === null) return;
    clearInterval(pollTimer);
    pollTimer = null;
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopPolling();
    } else {
      startPolling();
    }
  });

  // Start only if the tab is currently visible
  if (!document.hidden) {
    startPolling();
  }

  const clearBtn = document.getElementById('log-clear');
  if (clearBtn) {
    clearBtn.addEventListener('click', async () => {
      await fetch('/admin/logs/clear', { method: 'POST' });
      box.dataset.last = '';
      box.textContent = '(cleared)';
    });
  }
})();

// User storage stats
(function () {
  const loading = document.getElementById('user-stats-loading');
  const box = document.getElementById('user-stats-box');
  const tbody = document.getElementById('user-stats-body');
  const tfoot = document.getElementById('user-stats-foot');
  const refreshBtn = document.getElementById('user-stats-refresh');
  if (!loading || !box || !tbody) return;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[<>&]/g,
      c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
  }

  async function load() {
    loading.style.display = 'block';
    loading.textContent = 'Loading stats…';
    box.style.display = 'none';

    try {
      const r = await fetch('/admin/user-stats');
      if (!r.ok) {
        loading.textContent = 'Error: ' + r.status;
        return;
      }
      const d = await r.json();
      tbody.innerHTML = '';
      for (const u of d.users || []) {
        const tr = document.createElement('tr');
        tr.style.borderBottom = '1px solid #eee';
        tr.innerHTML =
          '<td style="padding:6px">' + esc(u.username) + '</td>' +
          '<td style="padding:6px;text-align:right">' + u.videos + '</td>' +
          '<td style="padding:6px;text-align:right">' + u.folders + '</td>' +
          '<td style="padding:6px;text-align:right">' + esc(u.bytes_human) + '</td>';
        tbody.appendChild(tr);
      }
      tfoot.innerHTML =
        '<tr style="border-top:2px solid #ddd;font-weight:600">' +
        '<td style="padding:8px">Total</td>' +
        '<td style="padding:8px;text-align:right">' + d.total_videos + '</td>' +
        '<td style="padding:8px;text-align:right"></td>' +
        '<td style="padding:8px;text-align:right">' + esc(d.total_bytes_human) + '</td>' +
        '</tr>';

      loading.style.display = 'none';
      box.style.display = 'block';
    } catch (e) {
      loading.textContent = 'Error: ' + e.message;
    }
  }

  if (refreshBtn) refreshBtn.addEventListener('click', load);
  load();
})();

// Admin archive controls
document.querySelectorAll('.admin-archive-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const uid = btn.dataset.uid;
    const action = btn.dataset.action;
    const status = document.querySelector(`.admin-archive-status[data-uid="${uid}"]`);

    if (!confirm('This will affect all downloads for this user. Continue?')) return;

    status.style.color = '#666';
    status.textContent = 'Working…';
    btn.disabled = true;

    try {
      let msg = '';
      if (action === 'clear' || action === 'clear-retrigger') {
        const r1 = await fetch(`/admin/clear-archive/${uid}`, { method: 'POST' });
        const d1 = await r1.json();
        msg = d1.message || 'Cleared.';
        if (!d1.ok) throw new Error(msg);
      }
      if (action === 'retrigger' || action === 'clear-retrigger') {
        const r2 = await fetch(`/admin/retrigger/${uid}`, { method: 'POST' });
        const d2 = await r2.json();
        msg += ' ' + (d2.message || 'Queued.');
        if (!d2.ok) throw new Error(msg);
      }
      status.style.color = '#0a5';
      status.textContent = msg;
    } catch (e) {
      status.style.color = '#c00';
      status.textContent = 'Error: ' + e.message;
    } finally {
      btn.disabled = false;
    }
  });
});
</script>

<p class="small"><a href="/">← Back to dashboard</a></p>
</body></html>
"""

EDIT_PAGE = """
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>ytfinall — Edit source</title><link rel="icon" type="image/png" href="/static/favicon.png">
<style>{{ css }}</style></head><body>
<img src="/static/logo.png" alt="ytfinall" class="logo">
<h2>Edit source</h2>

<form method="post" action="/edit/{{ s.id }}">
  <div class="field-group">
    <label>Source URL (read-only)</label>
    <input value="{{ s.url }}" disabled>
  </div>

  <div class="field-group">
    <label>Custom Name (optional)</label>
    <div class="input-row">
      <input name="name" value="{{ s.name or '' }}" placeholder="Leave blank to use the channel's name">
      <label for="edit-name" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="edit-name" class="info-toggle">
    <div class="info-box">
      <strong>What this does:</strong> Sets the display folder name inside your Jellyfin library. If left blank, the channel's actual YouTube name is used automatically.
    </div>
  </div>

  <div class="field-group">
    <label>Download Cutoff Date (optional)</label>
    <div class="input-row">
      <input name="cutoff" type="date" value="{{ s.cutoff or '' }}">
      <label for="edit-cutoff" class="info-btn">ⓘ</label>
    </div>
    <p class="small" style="margin-top:6px">Leave blank to download the last <strong>7 days</strong>.</p>
    <input type="checkbox" id="edit-cutoff" class="info-toggle">
    <div class="info-box">
      <strong>How it works:</strong>
      <ul>
        <li>Limits downloads to items posted <em>after</em> this specific target timestamp.</li>
        <li>If left blank, the default is the last <strong>7 days</strong>.</li>
        <li>The date cannot be earlier than your administrator's limit of <strong>{{ max_lookback }} days</strong>.</li>
      </ul>
    </div>
  </div>

  <div class="field-group">
    <label>Retention period (1–{{ max_retention }} days)</label>
    <div class="input-row">
      <input name="retention" type="number" min="1" max="{{ max_retention }}" value="{{ s.retention_days }}">
      <label for="edit-retention" class="info-btn">ⓘ</label>
    </div>
    <input type="checkbox" id="edit-retention" class="info-toggle">
    <div class="info-box">
      <strong>Storage Maintenance Tip:</strong> This controls how long downloaded media assets sit inside your active library before automated cache scrubbing deletes them. Your administrator enforces an upper tier threshold of <strong>{{ max_retention }} days</strong>.
    </div>
  </div>

  <button type="submit" style="margin-top: 10px;">Save changes</button>
</form>
<p class="small"><a href="/">← Back to dashboard</a></p>
</body></html>
"""

# ------------------------------------------------------------------
# Setup enforcement
# ------------------------------------------------------------------
@app.before_request
def require_setup():
    if request.endpoint in ("setup", "static"):
        return
    if not is_configured():
        return redirect("/setup")


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'",
    )
    return resp


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        jf_url = request.form["jellyfin_url"].strip().rstrip("/")
        api_key = request.form["jellyfin_api_key"].strip()
        try:
            lookback = max(1, int(request.form["max_lookback_days"]))
            retention = max(1, int(request.form["max_retention_days"]))
        except ValueError:
            return render_template_string(
                SETUP_PAGE, css=BASE_CSS,
                jf_url=jf_url, max_lookback=31, max_retention=31,
                error="Please enter valid numbers for the limits.",
            )

        try:
            r = requests.get(f"{jf_url}/System/Info",
                             headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
                             timeout=10)
            if r.status_code != 200:
                raise ValueError(f"Jellyfin returned HTTP {r.status_code}")
        except Exception as e:
            return render_template_string(
                SETUP_PAGE, css=BASE_CSS,
                jf_url=jf_url, max_lookback=lookback, max_retention=retention,
                error=f"Could not reach Jellyfin with that key: {e}",
            )

        set_config("jellyfin_url", jf_url)
        set_config("jellyfin_api_key", api_key)
        set_config("max_lookback_days", lookback)
        set_config("max_retention_days", retention)
        return redirect("/login")

    return render_template_string(
        SETUP_PAGE, css=BASE_CSS,
        jf_url=get_config("jellyfin_url", "http://host.docker.internal:8096"),
        max_lookback=max_lookback_days(),
        max_retention=max_retention_days(),
        error=None,
    )


@app.route("/")
def index():
    if "user_id" not in session:
        return redirect("/login")
    with db() as conn:
        sources = conn.execute(
            "SELECT * FROM sources WHERE user_id=? ORDER BY id DESC",
            (session["user_id"],),
        ).fetchall()
        urow = conn.execute(
            "SELECT delete_on_finish FROM users WHERE user_id=?",
            (session["user_id"],),
        ).fetchone()
    return render_template_string(
        DASHBOARD,
        css=BASE_CSS,
        username=session.get("username", "User"),
        sources=[s for s in sources if not is_single_video_url(s["url"])],
        is_admin=session.get("is_admin", False),
        max_lookback=max_lookback_days(),
        max_retention=max_retention_days(),
        delete_on_finish=bool(urow and urow["delete_on_finish"]),
        donation_links=DONATION_LINKS,
        donation_message=DONATION_MESSAGE,
    )


@app.route("/search")
@limiter.limit("30 per minute; 300 per hour")
def search():
    if "user_id" not in session:
        return redirect("/login")
    q = (request.args.get("q") or "").strip()
    kind = request.args.get("type") or "video"
    if kind not in ("video", "channel"):
        kind = "video"

    try:
        count = int(request.args.get("count") or 30)
    except ValueError:
        count = 30
    count = max(15, min(count, 300))

    results = []
    has_more = False
    if q:
        if kind == "channel":
            # YouTube's channel-scrape returns roughly one page; there's
            # no continuation token without a full player response, so we
            # just show everything we got in one go.
            results = _search_channels(q, limit=count)
        else:
            # Fetch one extra so we can tell whether more exist.
            fetched = _search_videos(q, limit=count + 1)
            has_more = len(fetched) > count
            results = fetched[:count]

    return render_template_string(
        SEARCH_PAGE,
        css=BASE_CSS,
        q=q,
        kind=kind,
        results=results,
        count=count,
        has_more=has_more,
        username=session.get("username", "User"),
        is_admin=session.get("is_admin", False),
        max_retention=max_retention_days(),
    )


@app.route("/channel/<channel_id>")
@limiter.limit("20 per minute; 200 per hour")
def channel_page(channel_id):
    if "user_id" not in session:
        return redirect("/login")
    if not re.match(r"^[A-Za-z0-9_-]{10,30}$", channel_id):
        return redirect("/")

    try:
        count = int(request.args.get("count") or 60)
    except ValueError:
        count = 60
    count = max(30, min(count, 500))

    # Fetch one extra so we can tell if more exist beyond the display cap.
    # yt-dlp sometimes returns fewer items than requested on a channel page,
    # so we treat "any results came back and we're under the cap" as has_more.
    fetched = _list_channel_videos(channel_id, count=count + 1)
    has_more = len(fetched) > 0 and count < 500
    videos = fetched[:count]
    info = _channel_info(channel_id)
    return render_template_string(
        CHANNEL_PAGE,
        css=BASE_CSS,
        channel_id=channel_id,
        channel_title=info.get("title") or channel_id,
        channel_avatar=info.get("thumbnail") or "",
        videos=videos,
        count=count,
        has_more=has_more,
        username=session.get("username", "User"),
        is_admin=session.get("is_admin", False),
        max_retention=max_retention_days(),
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        uid, _token = jellyfin_login(
            request.form["username"], request.form["password"]
        )
        if uid:
            session["user_id"] = uid
            session["username"] = request.form["username"]
            with db() as conn:
                # Ensure the user row exists.
                conn.execute(
                    "INSERT OR IGNORE INTO users (user_id, username, is_admin) "
                    "VALUES (?,?,0)",
                    (uid, request.form["username"]),
                )
                # Atomic first-admin promotion: only fires when this user
                # is the sole row. Simultaneous first logins cannot both win.
                conn.execute(
                    "UPDATE users SET is_admin=1 "
                    "WHERE user_id=? AND (SELECT COUNT(*) FROM users)=1",
                    (uid,),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT is_admin FROM users WHERE user_id=?", (uid,)
                ).fetchone()
                session["is_admin"] = bool(row and row["is_admin"])
            threading.Thread(
                target=ensure_user_library,
                args=(uid, request.form["username"]),
                daemon=True,
            ).start()
            return redirect("/")
        return render_template_string(
            LOGIN_PAGE, css=BASE_CSS,
            error="Login failed. Check your Jellyfin credentials.",
        )
    return render_template_string(LOGIN_PAGE, css=BASE_CSS, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/add", methods=["POST"])
def add_source():
    if "user_id" not in session:
        return redirect("/login")

    url = request.form["url"].strip()
    name = (request.form.get("name") or "").strip() or None
    cutoff = (request.form.get("cutoff") or "").strip() or None
    if cutoff:
        ok = False
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                datetime.datetime.strptime(cutoff, fmt)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            cutoff = None  # silently drop bad input

    try:
        retention = int(request.form.get("retention") or max_retention_days())
    except ValueError:
        retention = max_retention_days()
    retention = min(max(1, retention), max_retention_days())

    with db() as conn:
        conn.execute(
            "INSERT INTO sources (user_id, url, name, cutoff, retention_days) "
            "VALUES (?,?,?,?,?)",
            (session["user_id"], url, name, cutoff, retention),
        )

    if not enqueue_download(session["user_id"], url, name, cutoff):
        return "Download queue is full. Try again shortly.", 503
    return redirect("/")


@app.route("/delete/<int:source_id>", methods=["POST"])
def delete_source(source_id):
    if "user_id" not in session:
        return redirect("/login")
    with db() as conn:
        conn.execute(
            "DELETE FROM sources WHERE id=? AND user_id=?",
            (source_id, session["user_id"]),
        )
    return redirect("/")


@app.route("/edit/<int:source_id>", methods=["GET", "POST"])
def edit_source(source_id):
    if "user_id" not in session:
        return redirect("/login")

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM sources WHERE id=? AND user_id=?",
            (source_id, session["user_id"]),
        ).fetchone()

    if not row:
        return redirect("/")

    if request.method == "POST":
        name = (request.form.get("name") or "").strip() or None
        cutoff = (request.form.get("cutoff") or "").strip() or None
        if cutoff:
            ok = False
            for fmt in ("%Y-%m-%d", "%Y%m%d"):
                try:
                    datetime.datetime.strptime(cutoff, fmt)
                    ok = True
                    break
                except ValueError:
                    continue
            if not ok:
                cutoff = None  # silently drop bad input
        try:
            retention = int(request.form.get("retention") or max_retention_days())
        except ValueError:
            retention = max_retention_days()
        retention = min(max(1, retention), max_retention_days())

        with db() as conn:
            conn.execute(
                "UPDATE sources SET name=?, cutoff=?, retention_days=? "
                "WHERE id=? AND user_id=?",
                (name, cutoff, retention, source_id, session["user_id"]),
            )
        return redirect("/")

    return render_template_string(
        EDIT_PAGE,
        css=BASE_CSS,
        s=row,
        max_lookback=max_lookback_days(),
        max_retention=max_retention_days(),
    )


@app.route("/upload-cookies", methods=["POST"])
def upload_cookies():
    if "user_id" not in session or not session.get("is_admin"):
        return {"ok": False, "message": "Admin only."}, 403
    if not session.get("settings_unlocked"):
        return {"ok": False, "message": "Unlock settings first."}, 403

    file = request.files.get("cookies")
    if not file:
        return {"ok": False, "message": "No file received."}, 400

    if not file.filename.lower().endswith(".txt"):
        return {"ok": False, "message": "File must be a .txt"}, 400

    raw = file.read()
    # Parse as a Netscape cookie file; reject anything malformed.
    import http.cookiejar as _cj
    import io as _io
    try:
        jar = _cj.MozillaCookieJar()
        jar._really_load(
            _io.StringIO(raw.decode("utf-8", errors="replace")),
            str(COOKIES_FILE),
            ignore_discard=True,
            ignore_expires=True,
        )
    except Exception:
        return {"ok": False,
                "message": "File is not a valid Netscape cookies.txt export."}, 400

    domains = {c.domain.lower() for c in jar}
    if not any("youtube.com" in d or "google.com" in d for d in domains):
        return {"ok": False,
                "message": "No YouTube/Google cookies found in this file."}, 400

    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(COOKIES_FILE):
        try:
            shutil.copy2(COOKIES_FILE, COOKIES_FILE + ".bak")
        except OSError:
            pass
    with open(COOKIES_FILE, "wb") as f:
        f.write(raw)
    try:
        os.chmod(COOKIES_FILE, 0o600)
        if os.path.exists(COOKIES_FILE + ".bak"):
            os.chmod(COOKIES_FILE + ".bak", 0o600)
    except OSError:
        pass

    print(f"[cookies] wrote {len(raw)} bytes to {COOKIES_FILE}", flush=True)
    return {"ok": True, "message": f"Saved cookies.txt ({len(raw)} bytes)."}


@app.route("/jellyfin/webhook", methods=["POST"])
def jellyfin_webhook():
    """Receive PlaybackStop events from the Jellyfin Webhook plugin.

    Configure Jellyfin with the full URL including ?token=... so random
    traffic can't trigger deletions.
    """
    import hmac as _hmac
    expected = webhook_secret()
    token = request.args.get("token", "")
    if not token or not _hmac.compare_digest(token, expected):
        return {"status": "error", "message": "unauthorized"}, 403

    data = request.get_json(silent=True)
    if not data:
        return {"status": "error", "message": "no json"}, 400

    if data.get("NotificationType") != "PlaybackStop":
        return {"status": "ignored", "reason": "not a stop event"}, 200
    p2c = str(data.get("PlayedToCompletion", "")).strip().lower()
    if p2c not in ("true", "1", "yes"):
        return {"status": "ignored", "reason": "not played to completion"}, 200

    raw_user_id = data.get("UserId") or ""
    item_path = data.get("Path") or ""
    item_name = data.get("Name") or "?"
    username = data.get("UserName") or ""

    if not raw_user_id:
        return {"status": "error", "message": "missing UserId"}, 400

    # Match the Jellyfin UUID to our stored no-dash form.
    urow = _find_user_row(raw_user_id)
    if not urow:
        print(f"[webhook] unknown user_id from Jellyfin: {raw_user_id}", flush=True)
        return {"status": "ignored", "message": "unknown user"}, 200

    # Use the stored form for all downstream lookups.
    user_id = urow["user_id"]
    if not username:
        username = urow["username"] or user_id

    # Some Jellyfin Webhook plugin versions don't populate {{Path}} in
    # the template. When that happens, search the user's media folder
    # for a file matching the title.
    if not item_path:
        item_path = _find_media_by_name(user_id, item_name)
        if not item_path:
            print(f"[webhook] could not locate media for {item_name!r} "
                  f"(user {user_id})", flush=True)
            return {"status": "error",
                    "message": "no Path in payload and lookup failed"}, 404

    if not urow["delete_on_finish"]:
        return {"status": "ok", "message": "auto-delete disabled for this user"}, 200

    # Path safety: only delete inside MEDIA_ROOT.
    real_root = os.path.realpath(MEDIA_ROOT)
    real_path = os.path.realpath(item_path)
    if not real_path.startswith(real_root + os.sep):
        print(f"[webhook] refusing to delete outside media root: {real_path}", flush=True)
        return {"status": "error", "message": "path not under media root"}, 400

    # Ownership safety: only delete files ytfinall downloaded. The
    # .ytfinall.json sidecar is written by the download pipeline and
    # by nothing else, so its presence proves the file is ours.
    # Videos with no marker are either legacy downloads or came from
    # some other source entirely — leave them alone.
    marker = real_path + ".ytfinall.json"
    if not os.path.exists(marker):
        print(f"[webhook] refusing to delete unmarked file: {real_path}", flush=True)
        return {"status": "ignored",
                "reason": "no ytfinall marker alongside file"}, 200

    if not os.path.exists(real_path):
        return {"status": "ok", "message": "already gone"}, 200

    deleted = []
    try:
        os.remove(real_path)
        deleted.append(os.path.basename(real_path))
        base, _ = os.path.splitext(real_path)
        # NFO and info.json sit alongside the file as sibling basenames
        # (video.nfo), while the ytfinall marker appends to the full
        # filename including extension (video.mp4.ytfinall.json).
        for side in (
            base + ".nfo",
            base + ".info.json",
            real_path + ".ytfinall.json",
        ):
            if os.path.exists(side):
                os.remove(side)
                deleted.append(os.path.basename(side))
    except OSError as e:
        print(f"[webhook] delete failed for {real_path}: {e}", flush=True)
        return {"status": "error", "message": "delete failed"}, 500

    print(f"[webhook] {username} finished {item_name!r} — removed {deleted}", flush=True)

    with db() as conn:
        lrow = conn.execute(
            "SELECT library_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    if lrow and lrow["library_id"]:
        refresh_jellyfin_library(lrow["library_id"])

    return {"status": "ok", "message": "deleted", "files": deleted}, 200


@app.route("/toggle-delete-on-finish", methods=["POST"])
def toggle_delete_on_finish():
    if "user_id" not in session:
        return redirect("/login")
    with db() as conn:
        conn.execute(
            "UPDATE users SET delete_on_finish = 1 - COALESCE(delete_on_finish, 0) "
            "WHERE user_id=?",
            (session["user_id"],),
        )
    return redirect("/")


@app.route("/admin/logs/clear", methods=["POST"])
def admin_logs_clear():
    if "user_id" not in session or not session.get("is_admin"):
        return {"ok": False}, 403
    if not session.get("settings_unlocked"):
        return {"ok": False}, 403
    with _log_lock:
        _log_buffer.clear()
    return {"ok": True}, 200


# (debug/test endpoints removed — they exposed internal state and
# were not needed in production.)

@app.route("/admin/logs")
def admin_logs():
    """Return the last N buffered log lines as JSON."""
    if "user_id" not in session or not session.get("is_admin"):
        return {"error": "admin only"}, 403
    if not session.get("settings_unlocked"):
        return {"error": "settings locked"}, 403
    with _log_lock:
        lines = list(_log_buffer)
    return {"lines": lines}, 200


@app.route("/admin/user-stats")
def admin_user_stats():
    """Return video count + storage for each user. Admin only."""
    if "user_id" not in session or not session.get("is_admin"):
        return {"error": "admin only"}, 403
    if not session.get("settings_unlocked"):
        return {"error": "settings locked"}, 403

    with db() as conn:
        users = conn.execute(
            "SELECT user_id, username FROM users ORDER BY username"
        ).fetchall()

    results = []
    for u in users:
        username = u["username"] or u["user_id"]
        stats = get_user_stats(username)
        results.append({
            "username": username,
            "videos": stats["videos"],
            "folders": stats["folders"],
            "bytes": stats["bytes"],
            "bytes_human": _fmt_bytes(stats["bytes"]),
        })

    total_videos = sum(r["videos"] for r in results)
    total_bytes = sum(r["bytes"] for r in results)

    return {
        "users": results,
        "total_videos": total_videos,
        "total_bytes": total_bytes,
        "total_bytes_human": _fmt_bytes(total_bytes),
    }, 200


@app.route("/admin/clear-archive/<user_id>", methods=["POST"])
def clear_archive(user_id):
    """Delete the download archive for a user so yt-dlp re-downloads everything."""
    if "user_id" not in session or not session.get("is_admin"):
        return {"ok": False, "message": "Admin only."}, 403
    if not session.get("settings_unlocked"):
        return {"ok": False, "message": "Unlock settings first."}, 403

    archive = f"/app-data/{user_id}/archive.txt"
    count = 0
    if os.path.exists(archive):
        with open(archive) as f:
            count = sum(1 for line in f if line.strip())
        os.remove(archive)
        print(f"[admin] cleared archive for {user_id} ({count} entries)", flush=True)

    return {"ok": True, "message": f"Cleared {count} entries from archive."}, 200


@app.route("/admin/retrigger/<user_id>", methods=["POST"])
def retrigger_user(user_id):
    """Re-run every source for a user in the background."""
    if "user_id" not in session or not session.get("is_admin"):
        return {"ok": False, "message": "Admin only."}, 403
    if not session.get("settings_unlocked"):
        return {"ok": False, "message": "Unlock settings first."}, 403

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM sources WHERE user_id=?", (user_id,)
        ).fetchall()

    queued = 0
    for row in rows:
        if enqueue_download(row["user_id"], row["url"],
                            row["name"], row["cutoff"]):
            queued += 1

    print(f"[admin] retriggered {queued}/{len(rows)} sources for {user_id}", flush=True)
    return {"ok": True, "message": f"Queued {queued} of {len(rows)} sources for rescan."}, 200


def require_admin():
    """Abort unless the current session belongs to an admin."""
    if "user_id" not in session:
        abort(401)
    if not session.get("is_admin"):
        abort(403)


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        return redirect("/login")
    if not session.get("is_admin"):
        return redirect("/")

    locked = not session.get("settings_unlocked")

    if request.method == "POST":
        if locked:
            entered = request.form.get("unlock_key", "").strip()
            if entered == jellyfin_api_key():
                session["settings_unlocked"] = True
                return redirect("/settings")
            return render_template_string(
                SETTINGS_PAGE, css=BASE_CSS, locked=True,
                error="That key doesn't match.",
                jf_url=jellyfin_url(), api_key="",
                user_list=[],
                max_lookback=max_lookback_days(),
                max_retention=max_retention_days(),
                playlist_end=playlist_end(),
                sleep_requests=sleep_requests(),
                sleep_interval=sleep_interval(),
                max_sleep_interval=max_sleep_interval(),
                max_resolution=max_resolution(),
                media_container=media_container(),
                outtmpl=outtmpl_setting(),
                extra_ytdlp_args=get_config("extra_ytdlp_args", ""),
                index_interval_hours=index_interval_hours(),
                cleanup_interval_hours=cleanup_interval_hours(),
                cookies_present=os.path.exists(COOKIES_FILE), ok=None,
            )

        def _int_field(name, default, lo=0, hi=100000):
            try:
                v = int(request.form.get(name, default))
            except (TypeError, ValueError):
                v = default
            return max(lo, min(v, hi))

        set_config("jellyfin_url", request.form["jellyfin_url"].strip().rstrip("/"))
        set_config("jellyfin_api_key", request.form["jellyfin_api_key"].strip())
        set_config("max_lookback_days",      _int_field("max_lookback_days", max_lookback_days(), 1, 3650))
        set_config("max_retention_days",     _int_field("max_retention_days", max_retention_days(), 1, 3650))
        set_config("playlist_end",           _int_field("playlist_end", playlist_end(), 1, 500))
        set_config("sleep_requests",         _int_field("sleep_requests", sleep_requests(), 0, 60))
        set_config("sleep_interval",         _int_field("sleep_interval", sleep_interval(), 0, 600))
        set_config("max_sleep_interval",     _int_field("max_sleep_interval", max_sleep_interval(), 0, 600))
        set_config("max_resolution",         _int_field("max_resolution", max_resolution(), 144, 4320))
        set_config("media_container",        request.form.get("media_container", media_container()).strip() or "mp4")
        set_config("outtmpl",                request.form.get("outtmpl", outtmpl_setting()).strip() or outtmpl_setting())
        set_config("extra_ytdlp_args",       request.form.get("extra_ytdlp_args", "").strip())
        set_config("index_interval_hours",   _int_field("index_interval_hours", index_interval_hours(), 1, 720))
        set_config("cleanup_interval_hours", _int_field("cleanup_interval_hours", cleanup_interval_hours(), 1, 720))

        return render_template_string(
            SETTINGS_PAGE, css=BASE_CSS, locked=False,
            jf_url=jellyfin_url(), api_key=jellyfin_api_key(),
            user_list=build_user_list(),
            max_lookback=max_lookback_days(),
            max_retention=max_retention_days(),
            playlist_end=playlist_end(),
            sleep_requests=sleep_requests(),
            sleep_interval=sleep_interval(),
            max_sleep_interval=max_sleep_interval(),
            max_resolution=max_resolution(),
            media_container=media_container(),
            outtmpl=outtmpl_setting(),
            extra_ytdlp_args=get_config("extra_ytdlp_args", ""),
            index_interval_hours=index_interval_hours(),
            cleanup_interval_hours=cleanup_interval_hours(),
            cookies_present=os.path.exists(COOKIES_FILE),
            ok="Settings saved.", error=None,
        )

    return render_template_string(
        SETTINGS_PAGE, css=BASE_CSS, locked=locked,
        jf_url=jellyfin_url(),
        api_key=jellyfin_api_key() if not locked else "",
        user_list=build_user_list() if not locked else [],
        max_lookback=max_lookback_days(),
        max_retention=max_retention_days(),
        playlist_end=playlist_end(),
        sleep_requests=sleep_requests(),
        sleep_interval=sleep_interval(),
        max_sleep_interval=max_sleep_interval(),
        max_resolution=max_resolution(),
        media_container=media_container(),
        outtmpl=outtmpl_setting(),
        extra_ytdlp_args=get_config("extra_ytdlp_args", ""),
        index_interval_hours=index_interval_hours(),
        cleanup_interval_hours=cleanup_interval_hours(),
        cookies_present=os.path.exists(COOKIES_FILE),
        ok=None, error=None,
    )


# ------------------------------------------------------------------
# Browser Extension API
# ------------------------------------------------------------------

@app.route("/api/tasks/clear", methods=["POST"])
def api_tasks_clear():
    if "user_id" not in session:
        return {"ok": False}, 403
    with db() as conn:
        conn.execute(
            "DELETE FROM download_tasks WHERE user_id=? AND finished_at IS NOT NULL",
            (session["user_id"],),
        )
    return {"ok": True}, 200


@app.route("/api/tasks")
def api_tasks():
    if "user_id" not in session:
        return {"tasks": []}, 200
    tasks = get_user_tasks(session["user_id"], limit=5)
    return {"tasks": tasks}, 200


def _hash_token(raw: str) -> str:
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_user_id(uid):
    """Jellyfin sends UUIDs with dashes; ytfinall stores them without.

    Normalize to lowercase, no-dash form for comparisons.
    """
    return (uid or "").replace("-", "").lower()


def _find_user_row(user_id):
    """Look up a user by ID regardless of dashed/undashed format."""
    norm = _normalize_user_id(user_id)
    with db() as conn:
        return conn.execute(
            "SELECT user_id, username, delete_on_finish FROM users "
            "WHERE REPLACE(LOWER(user_id), '-', '') = ?",
            (norm,),
        ).fetchone()


def _find_media_by_name(user_id, name):
    """Locate a media file in the user's library by fuzzy-matching the title.

    Used as a fallback when Jellyfin's webhook payload lacks a Path field.
    ytfinall's filenames always contain the video title, so a substring
    match on the sanitized title finds it reliably.
    """
    if not name:
        return ""
    row = _find_user_row(user_id)
    if not row or not row["username"]:
        return ""
    username = safe_username(row["username"])
    root = f"{MEDIA_ROOT}/{username}/shows"
    if not os.path.isdir(root):
        return ""

    # Strip filesystem-illegal chars the same way yt-dlp does when writing.
    needle = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).lower().strip()
    if not needle:
        return ""

    media_exts = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v"}
    matches = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if os.path.splitext(f)[1].lower() not in media_exts:
                continue
            if needle in f.lower():
                matches.append(os.path.join(dirpath, f))

    if not matches:
        return ""
    if len(matches) == 1:
        return matches[0]
    # Multiple matches — prefer the most recently modified.
    matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return matches[0]


def issue_api_token(user_id: str, username: str, ttl_days: int = 30) -> str:
    """Mint an opaque token, store only its hash, return the raw value once."""
    import secrets
    raw = secrets.token_urlsafe(32)
    now = datetime.datetime.now(datetime.UTC)
    exp = now + datetime.timedelta(days=ttl_days)
    with db() as conn:
        conn.execute(
            "INSERT INTO api_tokens "
            "(token_hash, user_id, username, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (_hash_token(raw), user_id, username,
             now.isoformat(timespec="seconds"),
             exp.isoformat(timespec="seconds")),
        )
    return raw


def validate_api_token(raw: str):
    """Return (user_id, username) if the token is valid and unexpired."""
    if not raw:
        return None, None
    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    th = _hash_token(raw)
    with db() as conn:
        row = conn.execute(
            "SELECT user_id, username, expires_at FROM api_tokens "
            "WHERE token_hash=?",
            (th,),
        ).fetchone()
    if not row:
        return None, None
    if row["expires_at"] and row["expires_at"] < now:
        with db() as conn:
            conn.execute("DELETE FROM api_tokens WHERE token_hash=?", (th,))
        return None, None
    return row["user_id"], row["username"]


@app.route("/api/extension-login", methods=["POST"])
@limiter.limit("5 per minute; 20 per hour")
def api_extension_login():
    """Trade Jellyfin credentials for our own opaque token."""
    data = request.get_json(silent=True)
    if not data or not data.get("username") or not data.get("password"):
        return jsonify({"error": "Username and password required"}), 400

    uid, _jellyfin_token = jellyfin_login(data["username"], data["password"])
    if not uid:
        # Generic error; do not reveal whether the username exists.
        return jsonify({"error": "Invalid credentials"}), 401

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, is_admin) "
                "VALUES (?,?,0)",
                (uid, data["username"]),
            )
            threading.Thread(
                target=ensure_user_library,
                args=(uid, data["username"]),
                daemon=True,
            ).start()

    raw_token = issue_api_token(uid, data["username"])
    return jsonify({
        "token": raw_token,
        "user_id": uid,
        "username": data["username"],
    }), 200


@app.route("/api/download", methods=["POST"])
@limiter.limit("10 per minute; 100 per day")
def api_download():
    """Endpoint for the browser extension to queue a download."""
    data = request.get_json(silent=True)
    if not data or not data.get("url") or not data.get("token"):
        return jsonify({"error": "url and token are required"}), 400

    target_url = data["url"].strip()
    user_token = data["token"].strip()

    # Validate OUR opaque token, not a raw Jellyfin token.
    user_id, username = validate_api_token(user_token)
    if not user_id:
        return jsonify({"error": "Authentication failed"}), 401

    # Ensure the user exists locally
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, is_admin) "
                "VALUES (?,?,0)",
                (user_id, username),
            )
            threading.Thread(
                target=ensure_user_library,
                args=(user_id, username),
                daemon=True,
            ).start()

    # Restrict API downloads to Jellyfin admins. Non-admin extension use
    # requires maintaining an allowlist table instead.
    try:
        jf_user_id, _ = jellyfin_login(username, "__never_matches__")
    except Exception:
        pass
    # Simpler path: check the local is_admin flag.
    with db() as conn:
        local = conn.execute(
            "SELECT is_admin FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    if not (local and local["is_admin"]):
        return jsonify({"error": "Not authorized to queue downloads"}), 403

    # Determine type and queue download
    is_playlist_only = (
        "/playlist" in target_url
        or ("list=" in target_url and "watch?v=" not in target_url and "youtu.be/" not in target_url)
    )
    is_single = (
        ("youtube.com/watch" in target_url or "youtu.be/" in target_url)
        and not is_playlist_only
    )
    url_type = "video" if is_single else "channel"

    print(f"[api] {username} queued {url_type}: {target_url}", flush=True)
    if not enqueue_download(user_id, target_url):
        return jsonify({"error": "Download queue is full. Try again shortly."}), 503

    return jsonify({
        "status": "queued",
        "type_detected": url_type,
        "message": f"Processing {url_type} for {username}.",
    }), 200


if __name__ == "__main__":
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    # Production WSGI server. Requires `pip install waitress` at image
    # build time.
    from waitress import serve
    serve(app, host="0.0.0.0", port=6842, threads=8)
