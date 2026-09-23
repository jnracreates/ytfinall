import os, sqlite3, subprocess, threading, time, datetime, requests, shutil, re
from flask import Flask, request, redirect, render_template_string, session, jsonify
from flask_cors import CORS

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
CORS(app, resources={r"/api/*": {"origins": "*"}})


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
                _log_buffer.append(line)

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
                library_id TEXT
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
# yt-dlp auto-update (throttled)
# ------------------------------------------------------------------
_last_update = 0.0
_update_lock = threading.Lock()
_download_lock = threading.Lock()


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
        try:
            write_all_episode_nfos(staging_dir)
            fix_one_off_nfo(staging_dir)
            _merge_move(staging_dir, final_root)
            print(f"[download] moved {staging_dir} -> {final_root}", flush=True)
        except Exception as e:
            print(f"[download] move error: {e}", flush=True)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

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


def cleanup_user_media(user_id, retention_days):
    cap = max_retention_days()
    retention_days = min(max(1, int(retention_days or cap)), cap)
    cutoff = time.time() - (retention_days * 86400)
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
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
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
                    run_download(row["user_id"], row["url"],
                                 row["name"], row["cutoff"])
            except Exception as e:
                print(f"[scheduler] index error: {e}")
            last_index = now

        if now - last_cleanup > cleanup_interval_hours() * 3600:
            try:
                with db() as conn:
                    rows = conn.execute(
                        "SELECT DISTINCT user_id, retention_days FROM sources"
                    ).fetchall()
                for row in rows:
                    cleanup_user_media(row["user_id"], row["retention_days"])
            except Exception as e:
                print(f"[scheduler] cleanup error: {e}")
            last_cleanup = now

        time.sleep(60)


threading.Thread(target=scheduler_loop, daemon=True).start()


# ------------------------------------------------------------------
# Templates
# ------------------------------------------------------------------
BASE_CSS = """
*{box-sizing:border-box}
body{font-family:sans-serif;max-width:760px;margin:0 auto;padding:16px;color:#222;-webkit-text-size-adjust:100%}
h2{margin-bottom:0.3em;font-size:1.5em}
h3{font-size:1.2em;margin-top:1.5em}
input,select{width:100%;padding:12px;margin:6px 0 14px;font-size:16px;border:1px solid #ccc;border-radius:4px}
button{padding:12px 22px;background:#00a4dc;color:#fff;border:none;cursor:pointer;font-size:16px;border-radius:4px}
button:hover{background:#0088b8}
.error{color:#c00;background:#fee;padding:10px;border-radius:4px}
.ok{color:#0a5;background:#e7f8ee;padding:10px;border-radius:4px}
table{width:100%;border-collapse:collapse;margin-top:14px;font-size:14px}
td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}
.small{color:#666;font-size:0.9em}
.header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
label{display:block;font-weight:600;margin-top:6px}
.card{background:#f7f9fb;border:1px solid #e1e6eb;border-radius:6px;padding:14px 18px;margin:18px 0}
a{color:#00a4dc;text-decoration:none}
a:hover{text-decoration:underline}
.logo{display:block;max-width:220px;margin:0 auto 20px;height:auto}
.logo-sm{max-width:150px;margin:0;height:auto}

/* Info Tooltip Styles */
.input-row { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.input-row input, .input-row select { margin: 0; flex-grow: 1; }
.info-btn { background: #555; color: #fff; border: none; cursor: pointer; padding: 10px 14px; font-size: 15px; border-radius: 4px; font-weight: bold; white-space: nowrap; transition: background 0.2s; }
.info-btn:hover { background: #333; }
.info-toggle { display: none; }
.info-toggle:checked + .info-box { display: block; }
.info-box { display: none; background: #f0f4f8; border-left: 4px solid #00a4dc; padding: 12px 16px; margin: 8px 0 16px 0; border-radius: 0 4px 4px 0; font-size: 14px; color: #333; line-height: 1.5; }
.info-box ul { margin: 6px 0 0 0; padding-left: 20px; }
.info-box li { margin-bottom: 4px; }
.field-group { margin-bottom: 14px; }

/* Donation footer */
.donate-footer { margin-top: 40px; padding-top: 20px; border-top: 1px solid #e1e6eb; text-align: center; }
.donate-message { font-size: 14px; color: #444; margin-bottom: 12px; font-style: italic; }
.donate-title { font-size: 13px; color: #666; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
.donate-links { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
.donate-link { display: inline-block; padding: 8px 16px; background: #f7f9fb; border: 1px solid #e1e6eb; border-radius: 6px; color: #00a4dc; font-size: 14px; font-weight: 600; text-decoration: none; transition: background 0.15s, border-color 0.15s; }
.donate-link:hover { background: #e3f2fd; border-color: #00a4dc; text-decoration: none; }

/* Mobile responsive */
@media (max-width: 600px) {
  body { padding: 12px; margin: 0; }
  h2 { font-size: 1.3em; }
  h3 { font-size: 1.1em; }
  .logo { max-width: 160px; }
  .logo-sm { max-width: 120px; }
  .input-row { flex-wrap: wrap; }
  .input-row input, .input-row select { width: 100%; }
  .info-btn { width: 100%; text-align: center; padding: 8px; }
  table { font-size: 13px; }
  td,th { padding: 6px; }
  .header { flex-direction: column; align-items: flex-start; gap: 6px; }
  .header > div { width: 100%; }
  .donate-links { flex-direction: column; }
  .donate-link { width: 100%; text-align: center; }
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
.info-btn { background: #555; color: #fff; border: none; cursor: pointer; padding: 10px 14px; font-size: 15px; border-radius: 4px; font-weight: bold; white-space: nowrap; transition: background 0.2s; display: inline-block; text-align: center; line-height: 1.2; }
.info-btn:hover { background: #333; }
.info-toggle { display: none; }
.info-toggle:checked + .info-box { display: block; }
.info-box { display: none; background: #f0f4f8; border-left: 4px solid #00a4dc; padding: 12px 16px; margin: 8px 0 16px 0; border-radius: 0 4px 4px 0; font-size: 14px; color: #333; line-height: 1.5; box-sizing: border-box; }
.info-box ul { margin: 6px 0 0 0; padding-left: 20px; }
.info-box li { margin-bottom: 4px; }
</style>

<form method="post" action="/setup">
  <div class="field-group">
    <label>Jellyfin URL (as seen from inside the container)</label>
    <div class="input-row">
      <input name="jellyfin_url" value="{{ jf_url }}" required>
      <label for="setup-url" class="info-btn">ⓘ Info</label>
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
      <label for="setup-key" class="info-btn">ⓘ Info</label>
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
      <label for="setup-lookback" class="info-btn">ⓘ Info</label>
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
      <label for="setup-retention" class="info-btn">ⓘ Info</label>
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

<div id="tasks-container" style="display:none;margin:18px 0">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <h3 style="margin:0">Active downloads</h3>
    <button type="button" id="tasks-clear" style="background:#888;padding:6px 12px;font-size:13px">Clear finished</button>
  </div>
  <div id="tasks-list"></div>
</div>

<style>
  .task { background: #f7f9fb; border: 1px solid #e1e6eb; border-radius: 6px; padding: 12px 16px; margin: 10px 0; }
  .task-url { font-size: 12px; color: #666; word-break: break-all; margin-bottom: 6px; }
  .task-msg { font-size: 13px; color: #333; margin-bottom: 6px; }
  .task-bar { background: #e1e6eb; height: 18px; border-radius: 4px; overflow: hidden; }
  .task-fill { background: #00a4dc; height: 100%; width: 0%; transition: width 0.3s ease; }
  .task.complete .task-fill { background: #0a5; }
  .task.failed .task-fill { background: #c33; }
  .task-eta { font-size: 12px; color: #666; margin-top: 4px; }
</style>

<h3>Add a one-off video</h3>
<form method="post" action="/add">
  <div class="field-group">
    <label>Video URL</label>
    <div class="input-row">
      <input name="url" required placeholder="https://www.youtube.com/watch?v=...">
      <label for="info-oneoff" class="info-btn">ⓘ Info</label>
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
      <label for="info-oneoff-ret" class="info-btn">ⓘ Info</label>
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
      <label for="info-url" class="info-btn">ⓘ Info</label>
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
      <label for="info-name" class="info-btn">ⓘ Info</label>
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
      <label for="info-cutoff" class="info-btn">ⓘ Info</label>
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
      <label for="info-retention" class="info-btn">ⓘ Info</label>
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
<tr><th>Name</th><th>URL</th><th>Cutoff</th><th>Retention</th><th></th></tr>
{% for s in sources %}
<tr>
  <td>{{ s.name or "—" }}</td>
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
        <label for="settings-url" class="info-btn">ⓘ Info</label>
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
        <label for="settings-key" class="info-btn">ⓘ Info</label>
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
        <label for="settings-lookback" class="info-btn">ⓘ Info</label>
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
        <label for="settings-retention" class="info-btn">ⓘ Info</label>
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
        <label for="settings-playlist" class="info-btn">ⓘ Info</label>
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
        <label for="settings-sleepreq" class="info-btn">ⓘ Info</label>
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
        <label for="settings-sleepmin" class="info-btn">ⓘ Info</label>
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
        <label for="settings-sleepmax" class="info-btn">ⓘ Info</label>
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
        <label for="settings-res" class="info-btn">ⓘ Info</label>
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
        <label for="settings-container" class="info-btn">ⓘ Info</label>
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
        <label for="settings-outtmpl" class="info-btn">ⓘ Info</label>
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
        <label for="settings-extra" class="info-btn">ⓘ Info</label>
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
        <label for="settings-index" class="info-btn">ⓘ Info</label>
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
        <label for="settings-cleanup" class="info-btn">ⓘ Info</label>
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
      <label for="edit-name" class="info-btn">ⓘ Info</label>
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
      <label for="edit-cutoff" class="info-btn">ⓘ Info</label>
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
      <label for="edit-retention" class="info-btn">ⓘ Info</label>
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
    return render_template_string(
        DASHBOARD,
        css=BASE_CSS,
        username=session.get("username", "User"),
        sources=sources,
        is_admin=session.get("is_admin", False),
        max_lookback=max_lookback_days(),
        max_retention=max_retention_days(),
        donation_links=DONATION_LINKS,
        donation_message=DONATION_MESSAGE,
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
                count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
                if count == 0:
                    # First-ever user becomes admin and is persisted as such.
                    conn.execute(
                        "INSERT OR IGNORE INTO users (user_id, username, is_admin) "
                        "VALUES (?,?,1)",
                        (uid, request.form["username"]),
                    )
                    session["is_admin"] = True
                else:
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

    threading.Thread(
        target=run_download,
        args=(session["user_id"], url, name, cutoff),
        daemon=True,
    ).start()
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
    if b"youtube" not in raw.lower() and b"google" not in raw.lower():
        return {"ok": False,
                "message": "That doesn't look like a YouTube cookies file."}, 400

    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(COOKIES_FILE):
        try:
            shutil.copy2(COOKIES_FILE, COOKIES_FILE + ".bak")
        except OSError:
            pass
    with open(COOKIES_FILE, "wb") as f:
        f.write(raw)

    print(f"[cookies] wrote {len(raw)} bytes to {COOKIES_FILE}", flush=True)
    return {"ok": True, "message": f"Saved cookies.txt ({len(raw)} bytes)."}


@app.route("/admin/logs/clear", methods=["POST"])
def admin_logs_clear():
    if "user_id" not in session or not session.get("is_admin"):
        return {"ok": False}, 403
    if not session.get("settings_unlocked"):
        return {"ok": False}, 403
    with _log_lock:
        _log_buffer.clear()
    return {"ok": True}, 200


@app.route("/admin/logs/debug")
def admin_logs_debug():
    import sys
    return {
        "stdout_class": type(sys.stdout).__name__,
        "stdout_id": id(sys.stdout),
        "buffer_len": len(_log_buffer),
        "buffer_sample": list(_log_buffer)[-3:],
    }, 200


@app.route("/admin/logs/test")
def admin_logs_test():
    import sys
    before = len(_log_buffer)
    buf_id = id(_log_buffer)
    stdout_id = id(sys.stdout)
    # Direct write to test the Tee
    sys.stdout.write("[test] direct stdout write\n")
    sys.stdout.flush()
    after_write = len(_log_buffer)
    # Now via print()
    print("[test] via print()", flush=True)
    after_print = len(_log_buffer)
    return {
        "buffer_id": buf_id,
        "stdout_id": stdout_id,
        "stdout_class": type(sys.stdout).__name__,
        "len_before": before,
        "len_after_write": after_write,
        "len_after_print": after_print,
        "buffer_tail": list(_log_buffer)[-5:],
    }, 200


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

    for row in rows:
        threading.Thread(
            target=run_download,
            args=(row["user_id"], row["url"], row["name"], row["cutoff"]),
            daemon=True,
        ).start()

    print(f"[admin] retriggered {len(rows)} sources for {user_id}", flush=True)
    return {"ok": True, "message": f"Queued {len(rows)} sources for rescan."}, 200


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        return redirect("/login")

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


@app.route("/api/extension-login", methods=["POST"])
def api_extension_login():
    """Login for the browser extension. Trades Jellyfin credentials for a token."""
    data = request.get_json(silent=True)
    if not data or not data.get("username") or not data.get("password"):
        return jsonify({"error": "Username and password required"}), 400

    uid, token = jellyfin_login(data["username"], data["password"])
    if not uid or not token:
        return jsonify({"error": "Invalid Jellyfin credentials"}), 401

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (uid,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, is_admin) VALUES (?,?,0)",
                (uid, data["username"]),
            )
            threading.Thread(
                target=ensure_user_library,
                args=(uid, data["username"]),
                daemon=True,
            ).start()

    return jsonify({
        "token": token,
        "user_id": uid,
        "username": data["username"],
    }), 200


@app.route("/api/download", methods=["POST"])
def api_download():
    """Endpoint for the browser extension to queue a download."""
    data = request.get_json(silent=True)
    if not data or not data.get("url") or not data.get("token"):
        return jsonify({"error": "url and token are required"}), 400

    target_url = data["url"].strip()
    user_token = data["token"].strip()

    # Validate the token with Jellyfin
    try:
        resp = requests.get(
            f"{jellyfin_url()}/Users/Me",
            headers={"Authorization": f'MediaBrowser Token="{user_token}"'},
            timeout=10,
        )
        resp.raise_for_status()
        user_data = resp.json()
        user_id = user_data["Id"]
        username = user_data["Name"]
    except Exception as e:
        print(f"[api] Token validation failed: {e}", flush=True)
        return jsonify({"error": "Authentication failed"}), 401

    # Ensure the user exists locally
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO users (user_id, username, is_admin) VALUES (?,?,0)",
                (user_id, username),
            )
            threading.Thread(
                target=ensure_user_library,
                args=(user_id, username),
                daemon=True,
            ).start()

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
    threading.Thread(
        target=run_download,
        args=(user_id, target_url),
        daemon=True,
    ).start()

    return jsonify({
        "status": "queued",
        "type_detected": url_type,
        "message": f"Processing {url_type} for {username}.",
    }), 200


if __name__ == "__main__":
    import logging
    # Silence Werkzeug's per-request access log so `docker logs` stays
    # readable. Errors and warnings still print.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    app.run(host="0.0.0.0", port=6842)
