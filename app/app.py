import os
import re
import sqlite3
import subprocess
import threading
import queue
import uuid
import shutil
import random
import platform
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import datetime, time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from flask import Flask, render_template, request, jsonify, send_from_directory, abort, redirect, url_for, session, flash
from flask_socketio import SocketIO
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = Path("/srv/streambox")
DATA_DIR = Path("/srv/streambox/data")
DOWNLOAD_DIR = DATA_DIR / "downloads"
THUMB_DIR = DATA_DIR / "thumbnails"
POSTER_DIR = DATA_DIR / "posters"
SUBTITLE_DIR = DATA_DIR / "subtitles"
MUSIC_DIR = DATA_DIR / "music"
TRASH_DIR = DATA_DIR / "trash"
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "media.db"
QUEUE_FILE = DATA_DIR / "queue.txt"
COOKIE_FILE = Path("/srv/streambox/cookies/youtube_cookies.txt")

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "").strip()
OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_ALLOWED_CHAT_IDS = [x.strip() for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()]
DOWNLOAD_SCHEDULE_ENABLED = os.environ.get("DOWNLOAD_SCHEDULE_ENABLED", "0") == "1"
DOWNLOAD_WINDOW_START = os.environ.get("DOWNLOAD_WINDOW_START", "01:00")
DOWNLOAD_WINDOW_END = os.environ.get("DOWNLOAD_WINDOW_END", "06:00")
TRANSCODE_PS4 = os.environ.get("TRANSCODE_PS4", "0") == "1"

AVAILABLE_THEMES = {
    "netflix": "Netflix Grid",
    "posterwall": "Poster Wall",
    "compact": "Kompakte Liste",
    "tv": "TV / Konsole",
    "kids": "Kids Kacheln",
    "senior": "Senior / große Schrift",
    "blue": "Blue Cinema",
    "classic": "Classic",
    "matrix": "Matrix Terminal",
    "glass": "Glass Neon",
    "terminal": "Admin Terminal",
    "sunset": "Sunset",
    "oled": "OLED Schwarz",
    "library": "Bibliothek",
    "radio": "Radio / Musik",
}

for d in (DOWNLOAD_DIR, THUMB_DIR, POSTER_DIR, SUBTITLE_DIR, MUSIC_DIR, TRASH_DIR, BACKUP_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "raspi-secret-key")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")


VERSION_FILE = BASE_DIR / "VERSION"

def app_version():
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return "dev"

def git_output(args, timeout=20):
    try:
        res = subprocess.run(
            ["git"] + args,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        return res.returncode, res.stdout.strip()
    except Exception as e:
        return 1, str(e)

def git_update_status():
    current = app_version()
    branch_code, branch = git_output(["rev-parse", "--abbrev-ref", "HEAD"])
    local_code, local = git_output(["rev-parse", "--short", "HEAD"])
    remote_code, remote_url = git_output(["remote", "get-url", "origin"])

    fetch_code, fetch_out = git_output(["fetch", "--tags", "--quiet"], timeout=45)
    behind_code, behind = git_output(["rev-list", "--count", "HEAD..@{u}"])
    tags_code, latest_tag = git_output(["describe", "--tags", "--abbrev=0", "@{u}"])

    try:
        behind_count = int(behind.strip())
    except Exception:
        behind_count = None

    return {
        "version": current,
        "branch": branch if branch_code == 0 else "unbekannt",
        "commit": local if local_code == 0 else "unbekannt",
        "remote": remote_url if remote_code == 0 else "kein Git Remote",
        "fetch_ok": fetch_code == 0,
        "fetch_message": fetch_out,
        "behind": behind_count,
        "latest_tag": latest_tag if tags_code == 0 else "",
        "update_available": bool(behind_count and behind_count > 0),
    }


JOBS = {}
JOBS_LOCK = threading.Lock()
DOWNLOAD_QUEUE = queue.Queue()
WORKER_STARTED = False
PAUSED = False
CANCELLED = set()
TELEGRAM_STARTED = False


def db():
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return con


def column_exists(con, table, column):
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def add_column_if_missing(con, table, column, definition):
    if not column_exists(con, table, column):
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                allowed_categories TEXT DEFAULT '',
                min_age INTEGER DEFAULT 0,
                theme TEXT DEFAULT 'netflix',
                login_token TEXT,
                created_at TEXT NOT NULL
            )
        """)
        add_column_if_missing(con, "users", "role", "TEXT NOT NULL DEFAULT 'viewer'")
        add_column_if_missing(con, "users", "allowed_categories", "TEXT DEFAULT ''")
        add_column_if_missing(con, "users", "min_age", "INTEGER DEFAULT 0")
        add_column_if_missing(con, "users", "theme", "TEXT DEFAULT 'netflix'")
        add_column_if_missing(con, "users", "login_token", "TEXT")

        con.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                filename TEXT NOT NULL UNIQUE,
                thumbnail TEXT,
                poster TEXT,
                source_url TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT 'Filme',
                series TEXT,
                season INTEGER,
                episode INTEGER,
                age_rating INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0,
                deleted_at TEXT,
                description TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                year TEXT DEFAULT '',
                genre TEXT DEFAULT '',
                actors TEXT DEFAULT '',
                tmdb_id TEXT DEFAULT '',
                imdb_id TEXT DEFAULT '',
                file_hash TEXT,
                subtitle TEXT,
                created_at TEXT NOT NULL
            )
        """)
        for col, definition in [
            ("poster", "TEXT"), ("age_rating", "INTEGER DEFAULT 0"), ("is_deleted", "INTEGER DEFAULT 0"),
            ("deleted_at", "TEXT"), ("description", "TEXT DEFAULT ''"), ("tags", "TEXT DEFAULT ''"),
            ("year", "TEXT DEFAULT ''"), ("genre", "TEXT DEFAULT ''"), ("actors", "TEXT DEFAULT ''"),
            ("tmdb_id", "TEXT DEFAULT ''"), ("imdb_id", "TEXT DEFAULT ''"), ("file_hash", "TEXT"),
            ("subtitle", "TEXT")
        ]:
            add_column_if_missing(con, "videos", col, definition)

        con.execute("""
            CREATE TABLE IF NOT EXISTS download_jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                normalized_url TEXT NOT NULL,
                status TEXT NOT NULL,
                progress TEXT DEFAULT '0%',
                message TEXT,
                title TEXT,
                filename TEXT,
                category TEXT DEFAULT 'Filme',
                series TEXT,
                season INTEGER,
                episode INTEGER,
                age_rating INTEGER DEFAULT 0,
                created_by INTEGER,
                priority INTEGER DEFAULT 100,
                media_type TEXT DEFAULT 'video',
                playlist INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )
        """)
        add_column_if_missing(con, "download_jobs", "age_rating", "INTEGER DEFAULT 0")
        add_column_if_missing(con, "download_jobs", "created_by", "INTEGER")
        add_column_if_missing(con, "download_jobs", "priority", "INTEGER DEFAULT 100")
        add_column_if_missing(con, "download_jobs", "media_type", "TEXT DEFAULT 'video'")
        add_column_if_missing(con, "download_jobs", "playlist", "INTEGER DEFAULT 0")

        con.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER NOT NULL,
                video_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, video_id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS watch_history (
                user_id INTEGER NOT NULL,
                video_id INTEGER NOT NULL,
                position REAL DEFAULT 0,
                duration REAL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, video_id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS allowed_domains (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL UNIQUE,
                note TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        default_domains = [
            ("youtube.com", "YouTube"), ("m.youtube.com", "YouTube Mobile"),
            ("music.youtube.com", "YouTube Music"), ("youtu.be", "YouTube Kurzlink"),
            ("dailymotion.com", "Dailymotion"), ("dai.ly", "Dailymotion Kurzlink"),
            ("geo.dailymotion.com", "Dailymotion"), ("vimeo.com", "Vimeo"),
            ("soundcloud.com", "SoundCloud"), ("bandcamp.com", "Bandcamp"),
            ("mixcloud.com", "Mixcloud"), ("audiomack.com", "Audiomack"),
            ("twitch.tv", "Twitch"), ("clips.twitch.tv", "Twitch Clips"),
            ("peertube.tv", "PeerTube"),
        ]
        for domain, note in default_domains:
            con.execute("""
                INSERT OR IGNORE INTO allowed_domains(domain, note, enabled, created_at)
                VALUES (?, ?, 1, ?)
            """, (domain, note, datetime.utcnow().isoformat(timespec="seconds") + "Z"))

        admin_user = os.environ.get("ADMIN_USER", "admin")
        admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
        exists = con.execute("SELECT id FROM users WHERE username = ?", (admin_user,)).fetchone()
        if not exists:
            con.execute("""
                INSERT INTO users(username, password_hash, role, theme, login_token, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                admin_user,
                generate_password_hash(admin_password),
                "admin",
                "netflix",
                uuid.uuid4().hex,
                datetime.utcnow().isoformat(timespec="seconds") + "Z"
            ))
        else:
            token = con.execute("SELECT login_token FROM users WHERE username = ?", (admin_user,)).fetchone()["login_token"]
            if not token:
                con.execute("UPDATE users SET login_token = ? WHERE username = ?", (uuid.uuid4().hex, admin_user))


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with db() as con:
        return con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def deco(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user or user["role"] not in roles:
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco


def category_allowed(user, category):
    if not user:
        return False
    if user["role"] in ("admin", "downloader"):
        return True
    allowed = (user["allowed_categories"] or "").strip()
    if not allowed:
        return True
    cats = [x.strip().lower() for x in allowed.split(",") if x.strip()]
    return (category or "Filme").lower() in cats


def normalize_url(url):
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    host_key = host[4:] if host.startswith("www.") else host
    clean = parsed._replace(fragment="")
    return urlunparse(clean), host_key


def get_allowed_domains():
    with db() as con:
        rows = con.execute("SELECT domain FROM allowed_domains WHERE enabled = 1 ORDER BY domain").fetchall()
    return {r["domain"].lower().strip() for r in rows if r["domain"]}


def allowed_url(url):
    normalized, host_key = normalize_url(url)
    parsed = urlparse(normalized)
    if parsed.scheme not in ("http", "https"):
        return False

    allowed_hosts = get_allowed_domains()
    if host_key in allowed_hosts:
        return True

    return any(host_key.endswith("." + d) for d in allowed_hosts if d)


def clean_title(filename):
    title = Path(filename).stem
    title = re.sub(r"\s*\[[A-Za-z0-9_-]+\]\s*$", "", title)
    title = title.replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip()
    return title or "Video"


def parse_time_string(s):
    try:
        h, m = s.split(":")
        return time(int(h), int(m))
    except Exception:
        return time(0, 0)


def download_window_open():
    if not DOWNLOAD_SCHEDULE_ENABLED:
        return True
    now = datetime.now().time()
    start = parse_time_string(DOWNLOAD_WINDOW_START)
    end = parse_time_string(DOWNLOAD_WINDOW_END)
    if start <= end:
        return start <= now <= end
    return now >= start or now <= end


def set_job(job_id, **kwargs):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(kwargs)
        payload = dict(JOBS[job_id])

    db_fields = {}
    for key in ("status", "progress", "message", "title", "filename"):
        if key in kwargs:
            db_fields[key] = kwargs[key]
    if db_fields:
        sets = ", ".join([f"{k} = ?" for k in db_fields])
        vals = list(db_fields.values()) + [job_id]
        with db() as con:
            con.execute(f"UPDATE download_jobs SET {sets} WHERE id = ?", vals)

    socketio.emit("job_update", {"job_id": job_id, **payload})


def save_queue_file():
    with db() as con:
        rows = con.execute("""
            SELECT url FROM download_jobs
            WHERE status = 'queued'
            ORDER BY priority ASC, created_at ASC
        """).fetchall()
    QUEUE_FILE.write_text(
        "\n".join([r["url"] for r in rows]) + ("\n" if rows else ""),
        encoding="utf-8"
    )


def create_thumbnail(video_path):
    thumb_name = secure_filename(video_path.stem[:120]) + ".jpg"
    thumb_path = THUMB_DIR / thumb_name
    cmd = ["ffmpeg", "-y", "-ss", "00:00:05", "-i", str(video_path), "-frames:v", "1", "-q:v", "3", str(thumb_path)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if thumb_path.exists():
            return thumb_name
    except Exception:
        pass
    return None


def compute_hash(path, max_bytes=128 * 1024 * 1024):
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if read >= max_bytes:
                break
    return h.hexdigest()


def newest_mp4_after(start_ts):
    files = [f for f in DOWNLOAD_DIR.glob("*.mp4") if f.stat().st_mtime >= start_ts - 2]
    files = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def dlna_marker():
    marker = DATA_DIR / "dlna_rescan_requested.txt"
    marker.write_text(datetime.utcnow().isoformat(timespec="seconds") + "Z\n", encoding="utf-8")


def yt_dlp_command(url, out_tpl, media_type="video", playlist=False):
    normalized, host_key = normalize_url(url)

    cmd = [
        "yt-dlp",
        "--newline",
        "--force-ipv4",

        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    ]

    # -------------------------------------------------
    # DAILYMOTION FIX
    # -------------------------------------------------

    is_dailymotion = (
        host_key.endswith("dailymotion.com")
        or host_key in ("dai.ly", "geo.dailymotion.com")
    )

    if is_dailymotion:
        cmd.extend([
            "--add-header",
            "Referer:https://www.dailymotion.com/",

            "--add-header",
            "Origin:https://www.dailymotion.com",

            "--extractor-args",
            "dailymotion:client=web",
        ])

    # -------------------------------------------------
    # COOKIES NUR FÜR YOUTUBE
    # -------------------------------------------------

    is_youtube = (
        host_key.endswith("youtube.com")
        or host_key == "youtu.be"
        or host_key.endswith("googlevideo.com")
    )

    if COOKIE_FILE.exists() and is_youtube:
        cmd.extend([
            "--cookies",
            str(COOKIE_FILE)
        ])

    # -------------------------------------------------
    # AUDIO
    # -------------------------------------------------

    if media_type == "audio":

        if "soundcloud.com" in host_key:
            cmd.extend([
                "--ignore-errors",

                "--extractor-args",
                "soundcloud:formats=download,hls_opus,hls_mp3,progressive",
            ])

        cmd.extend([
            "-f",
            "bestaudio/best",

            "-x",

            "--audio-format",
            "mp3",

            "--audio-quality",
            "0",

            "--embed-thumbnail",

            "--add-metadata",

            "--restrict-filenames",

            "-o",
            str(MUSIC_DIR / "%(title).200s [%(id)s].%(ext)s"),
        ])

        if not playlist:
            cmd.append("--no-playlist")

        cmd.append(normalized)

        return cmd

    # -------------------------------------------------
    # VIDEO
    # -------------------------------------------------

    cmd.extend([
        "-f",
        "bv*+ba/b/best",

        "--merge-output-format",
        "mp4",

        "--write-subs",
        "--write-auto-subs",

        "--sub-langs",
        "de,en",

        "--convert-subs",
        "srt",

        "--restrict-filenames",

        "-o",
        out_tpl,
    ])

    if not playlist:
        cmd.append("--no-playlist")

    cmd.append(normalized)

    return cmd

    cmd.extend([
        "-f", "bv*+ba/b/best",
        "--merge-output-format", "mp4",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "de,en",
        "--convert-subs", "srt",
        "--restrict-filenames",
        "-o", out_tpl,
    ])

    if not playlist:
        cmd.append("--no-playlist")

    cmd.append(normalized)
    return cmd


def transcode_for_ps4(input_path):
    if not TRANSCODE_PS4:
        return input_path
    out = input_path.with_name(input_path.stem + ".ps4.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        str(out)
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=7200)
        if out.exists():
            input_path.unlink(missing_ok=True)
            return out
    except Exception:
        pass
    return input_path


def find_subtitle_for(video_path):
    candidates = list(video_path.parent.glob(video_path.stem + "*.srt"))
    if candidates:
        sub = candidates[0]
        dest = SUBTITLE_DIR / secure_filename(sub.name)
        try:
            shutil.move(str(sub), str(dest))
            return dest.name
        except Exception:
            return sub.name
    return None


def download_file(url, dest):
    try:
        urllib.request.urlretrieve(url, dest)
        return True
    except Exception:
        return False


def fetch_tmdb(title):
    if not TMDB_API_KEY:
        return {}
    try:
        q = urllib.parse.urlencode({"api_key": TMDB_API_KEY, "query": title, "language": "de-DE"})
        data = json.loads(urllib.request.urlopen(f"https://api.themoviedb.org/3/search/multi?{q}", timeout=10).read().decode("utf-8"))
        results = data.get("results") or []
        if not results:
            return {}
        r = results[0]
        details = {}
        media_type = r.get("media_type")
        tmdb_id = str(r.get("id") or "")
        if media_type in ("movie", "tv") and tmdb_id:
            detail_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=de-DE&append_to_response=credits"
            details = json.loads(urllib.request.urlopen(detail_url, timeout=10).read().decode("utf-8"))
        poster = r.get("poster_path") or details.get("poster_path")
        poster_name = None
        if poster:
            poster_name = secure_filename(f"tmdb_{tmdb_id}.jpg")
            download_file("https://image.tmdb.org/t/p/w500" + poster, POSTER_DIR / poster_name)
        genres = ", ".join([g.get("name","") for g in details.get("genres", []) if g.get("name")])
        actors = ", ".join([c.get("name","") for c in (details.get("credits", {}).get("cast") or [])[:8] if c.get("name")])
        return {
            "title": details.get("title") or details.get("name") or r.get("title") or r.get("name") or title,
            "description": details.get("overview") or r.get("overview") or "",
            "year": (details.get("release_date") or details.get("first_air_date") or r.get("release_date") or r.get("first_air_date") or "")[:4],
            "genre": genres,
            "actors": actors,
            "tmdb_id": tmdb_id,
            "poster": poster_name or ""
        }
    except Exception:
        return {}


def fetch_omdb(title):
    if not OMDB_API_KEY:
        return {}
    try:
        q = urllib.parse.urlencode({"apikey": OMDB_API_KEY, "t": title, "plot": "short"})
        data = json.loads(urllib.request.urlopen(f"https://www.omdbapi.com/?{q}", timeout=10).read().decode("utf-8"))
        if data.get("Response") != "True":
            return {}
        poster_name = None
        poster = data.get("Poster")
        if poster and poster != "N/A":
            poster_name = secure_filename(f"omdb_{data.get('imdbID','poster')}.jpg")
            download_file(poster, POSTER_DIR / poster_name)
        return {
            "title": data.get("Title") or title,
            "description": data.get("Plot") if data.get("Plot") != "N/A" else "",
            "year": data.get("Year") if data.get("Year") != "N/A" else "",
            "genre": data.get("Genre") if data.get("Genre") != "N/A" else "",
            "actors": data.get("Actors") if data.get("Actors") != "N/A" else "",
            "imdb_id": data.get("imdbID") or "",
            "poster": poster_name or ""
        }
    except Exception:
        return {}


def enrich_metadata(title):
    meta = fetch_tmdb(title)
    if not meta:
        meta = fetch_omdb(title)
    elif OMDB_API_KEY and not meta.get("actors"):
        other = fetch_omdb(title)
        for k, v in other.items():
            if not meta.get(k) and v:
                meta[k] = v
    return meta or {}


def run_download(job_id, url, category, series, season, episode, age_rating, created_by, media_type="video", playlist=False):
    global PAUSED
    while PAUSED or not download_window_open():
        set_job(job_id, status="queued", message=f"Wartet auf Download-Zeitfenster {DOWNLOAD_WINDOW_START}-{DOWNLOAD_WINDOW_END}")
        threading.Event().wait(30)

    if job_id in CANCELLED:
        set_job(job_id, status="cancelled", message="Abgebrochen")
        save_queue_file()
        return

    started_iso = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with db() as con:
        con.execute("UPDATE download_jobs SET status = ?, started_at = ? WHERE id = ?", ("running", started_iso, job_id))
    save_queue_file()

    set_job(job_id, status="running", progress="0%", message="Download startet")
    start_ts = datetime.utcnow().timestamp()
    out_tpl = str(DOWNLOAD_DIR / "%(title).200s [%(id)s].%(ext)s")

    cmd = yt_dlp_command(url, out_tpl, media_type=media_type, playlist=playlist)
    log = []

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        percent_re = re.compile(r"(\d+(?:\.\d+)?)%")

        for line in proc.stdout:
            if job_id in CANCELLED:
                proc.terminate()
                set_job(job_id, status="cancelled", message="Abgebrochen")
                with db() as con:
                    con.execute("UPDATE download_jobs SET status = ?, finished_at = ? WHERE id = ?",
                                ("cancelled", datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id))
                save_queue_file()
                return

            line = line.strip()
            if not line:
                continue
            log.append(line)
            log = log[-40:]

            if ("[download]" in line or "[Merger]" in line or "Extracting URL" in line or "[SubtitlesConvertor]" in line or "[dailymotion]" in line or "[soundcloud]" in line):
                set_job(job_id, message=line)
            m = percent_re.search(line)
            if m:
                set_job(job_id, progress=m.group(1) + "%")

        code = proc.wait()
        if code != 0:
            err = "\n".join(log[-15:]) or "Download fehlgeschlagen"
            set_job(job_id, status="error", message=err)
            with db() as con:
                con.execute("UPDATE download_jobs SET status = ?, message = ?, finished_at = ? WHERE id = ?",
                            ("error", err, datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id))
            save_queue_file()
            return

        if media_type == "audio":
            audio_files = [f for f in MUSIC_DIR.glob("*.mp3") if f.stat().st_mtime >= start_ts - 2]
            audio_files = sorted(audio_files, key=lambda f: f.stat().st_mtime, reverse=True)

            if not audio_files:
                msg = "Keine MP3-Datei gefunden"
                set_job(job_id, status="error", message=msg)
                with db() as con:
                    con.execute("UPDATE download_jobs SET status = ?, message = ?, finished_at = ? WHERE id = ?",
                                ("error", msg, datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id))
                save_queue_file()
                return

            audio_path = audio_files[0]
            title = clean_title(audio_path.name)
            file_hash = compute_hash(audio_path)

            with db() as con:
                dup = con.execute("SELECT id, filename FROM videos WHERE file_hash = ? AND is_deleted = 0", (file_hash,)).fetchone()
                if dup:
                    audio_path.unlink(missing_ok=True)
                    con.execute("UPDATE download_jobs SET status = ?, progress = ?, message = ?, finished_at = ? WHERE id = ?",
                                ("done", "100%", "Audio-Duplikat erkannt", datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id))
                    set_job(job_id, status="done", progress="100%", message="Audio-Duplikat erkannt")
                    save_queue_file()
                    return

                con.execute("""
                    INSERT OR IGNORE INTO videos
                    (title, filename, thumbnail, poster, source_url, category, series, season, episode, age_rating,
                     is_deleted, description, tags, year, genre, actors, tmdb_id, imdb_id, file_hash, subtitle, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    title, "music/" + audio_path.name, "", "", url,
                    category or "Musik", series or None,
                    int(season) if str(season).isdigit() else None,
                    int(episode) if str(episode).isdigit() else None,
                    int(age_rating) if str(age_rating).isdigit() else 0,
                    "Audio / MP3", "music,mp3", "", "Musik", "", "", "", file_hash, "",
                    datetime.utcnow().isoformat(timespec="seconds") + "Z"
                ))

                con.execute("""
                    UPDATE download_jobs
                    SET status = ?, progress = ?, message = ?, title = ?, filename = ?, finished_at = ?
                    WHERE id = ?
                """, (
                    "done", "100%", "Fertig", title, "music/" + audio_path.name,
                    datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id
                ))

            set_job(job_id, status="done", progress="100%", message="Fertig", title=title, filename="music/" + audio_path.name)
            save_queue_file()
            return

        video_path = newest_mp4_after(start_ts)
        if not video_path:
            msg = "Keine MP4-Datei gefunden"
            set_job(job_id, status="error", message=msg)
            with db() as con:
                con.execute("UPDATE download_jobs SET status = ?, message = ?, finished_at = ? WHERE id = ?",
                            ("error", msg, datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id))
            save_queue_file()
            return

        set_job(job_id, message="Berechne Dateihash")
        file_hash = compute_hash(video_path)
        with db() as con:
            dup = con.execute("SELECT id, filename FROM videos WHERE file_hash = ? AND is_deleted = 0", (file_hash,)).fetchone()
        if dup:
            video_path.unlink(missing_ok=True)
            set_job(job_id, status="done", progress="100%", message=f"Duplikat erkannt: {dup['filename']}")
            with db() as con:
                con.execute("UPDATE download_jobs SET status = ?, progress = ?, message = ?, finished_at = ? WHERE id = ?",
                            ("done", "100%", "Duplikat erkannt", datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id))
            save_queue_file()
            return

        set_job(job_id, message="Konvertiere PS4-kompatibel" if TRANSCODE_PS4 else "Erstelle Thumbnail")
        video_path = transcode_for_ps4(video_path)

        title = clean_title(video_path.name)
        thumb = create_thumbnail(video_path)
        subtitle = find_subtitle_for(video_path)

        set_job(job_id, message="Lade Metadaten")
        meta = enrich_metadata(title)
        final_title = meta.get("title") or title

        with db() as con:
            con.execute("""
                INSERT OR IGNORE INTO videos
                (title, filename, thumbnail, poster, source_url, category, series, season, episode, age_rating,
                 is_deleted, description, tags, year, genre, actors, tmdb_id, imdb_id, file_hash, subtitle, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                final_title, video_path.name, thumb, meta.get("poster") or "", url,
                category or "Filme",
                series or None,
                int(season) if str(season).isdigit() else None,
                int(episode) if str(episode).isdigit() else None,
                int(age_rating) if str(age_rating).isdigit() else 0,
                meta.get("description") or "",
                "",
                meta.get("year") or "",
                meta.get("genre") or "",
                meta.get("actors") or "",
                meta.get("tmdb_id") or "",
                meta.get("imdb_id") or "",
                file_hash,
                subtitle,
                datetime.utcnow().isoformat(timespec="seconds") + "Z"
            ))
            con.execute("""
                UPDATE download_jobs
                SET status = ?, progress = ?, message = ?, title = ?, filename = ?, finished_at = ?
                WHERE id = ?
            """, (
                "done", "100%", "Fertig", final_title, video_path.name,
                datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id
            ))

        set_job(job_id, status="done", progress="100%", message="Fertig", title=final_title, filename=video_path.name)
        save_queue_file()
        dlna_marker()

    except Exception as e:
        msg = str(e)
        set_job(job_id, status="error", message=msg)
        with db() as con:
            con.execute("UPDATE download_jobs SET status = ?, message = ?, finished_at = ? WHERE id = ?",
                        ("error", msg, datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id))
        save_queue_file()


def download_worker():
    while True:
        item = DOWNLOAD_QUEUE.get()
        try:
            run_download(**item)
        finally:
            DOWNLOAD_QUEUE.task_done()


def ensure_worker():
    global WORKER_STARTED
    if not WORKER_STARTED:
        t = threading.Thread(target=download_worker, daemon=True)
        t.start()
        WORKER_STARTED = True


def restore_queue_from_db():
    ensure_worker()
    with db() as con:
        rows = con.execute("""
            SELECT * FROM download_jobs
            WHERE status = 'queued'
            ORDER BY priority ASC, created_at ASC
        """).fetchall()
    for r in rows:
        DOWNLOAD_QUEUE.put({
            "job_id": r["id"],
            "url": r["url"],
            "category": r["category"] or "Filme",
            "series": r["series"] or "",
            "season": r["season"] or "",
            "episode": r["episode"] or "",
            "age_rating": r["age_rating"] or 0,
            "created_by": r["created_by"],
            "media_type": r["media_type"] or "video",
            "playlist": bool(r["playlist"] or 0),
        })
    save_queue_file()


def telegram_send(chat_id, text):
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=data, timeout=10)
    except Exception:
        pass


def enqueue_urls(urls, user_id=None, category="Telegram", series="", season="", episode="", age_rating=0, priority=100, media_type="video", playlist=False):
    created, skipped = [], []
    ensure_worker()
    with db() as con:
        for raw_url in urls:
            url, _ = normalize_url(raw_url)
            if not allowed_url(url):
                skipped.append({"url": raw_url, "reason": "Nicht erlaubt"})
                continue
            exists_video = con.execute("SELECT id FROM videos WHERE source_url = ? AND is_deleted = 0", (url,)).fetchone()
            exists_job = con.execute("SELECT id FROM download_jobs WHERE normalized_url = ? AND status IN ('queued','running','done')", (url,)).fetchone()
            if exists_video:
                skipped.append({"url": url, "reason": "Schon in Mediathek"})
                continue
            if exists_job:
                skipped.append({"url": url, "reason": "Schon in Queue oder fertig"})
                continue
            job_id = uuid.uuid4().hex
            created_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
            con.execute("""
                INSERT INTO download_jobs
                (id, url, normalized_url, status, progress, message, category, series, season, episode, age_rating, created_by, priority, media_type, playlist, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_id, url, url, "queued", "0%", "In Warteschlange", category, series or None,
                int(season) if str(season).isdigit() else None,
                int(episode) if str(episode).isdigit() else None,
                int(age_rating) if str(age_rating).isdigit() else 0,
                user_id, int(priority), media_type, 1 if playlist else 0, created_at
            ))
            DOWNLOAD_QUEUE.put({
                "job_id": job_id, "url": url, "category": category, "series": series,
                "season": season, "episode": episode, "age_rating": age_rating, "created_by": user_id,
                "media_type": media_type, "playlist": playlist
            })
            created.append({"job_id": job_id, "url": url})
    save_queue_file()
    return created, skipped


def telegram_worker():
    if not TELEGRAM_BOT_TOKEN:
        return
    offset = None
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?timeout=30"
            if offset:
                url += f"&offset={offset}"
            data = json.loads(urllib.request.urlopen(url, timeout=40).read().decode("utf-8"))
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id"))
                text = msg.get("text") or ""
                if TELEGRAM_ALLOWED_CHAT_IDS and chat_id not in TELEGRAM_ALLOWED_CHAT_IDS:
                    telegram_send(chat_id, "Nicht erlaubt.")
                    continue
                urls = re.findall(r"https?://\S+|(?:www\.)?(?:youtube\.com|youtu\.be|dailymotion\.com|vimeo\.com)/\S+", text)
                if urls:
                    created, skipped = enqueue_urls(urls, None, "Telegram")
                    telegram_send(chat_id, f"{len(created)} hinzugefuegt, {len(skipped)} uebersprungen.")
                elif text.startswith("/start"):
                    telegram_send(chat_id, "Sende mir einen YouTube/Dailymotion/Vimeo Link.")
        except Exception:
            threading.Event().wait(10)


def start_telegram():
    global TELEGRAM_STARTED
    if TELEGRAM_BOT_TOKEN and not TELEGRAM_STARTED:
        TELEGRAM_STARTED = True
        threading.Thread(target=telegram_worker, daemon=True).start()


@app.context_processor
def inject_user():
    return {"me": current_user(), "available_themes": AVAILABLE_THEMES, "APP_VERSION": app_version()}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with db() as con:
            user = con.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        return render_template("login.html", error="Login fehlgeschlagen")
    return render_template("login.html", error=None)


@app.route("/qr-login/<token>")
def qr_login(token):
    with db() as con:
        user = con.execute("SELECT * FROM users WHERE login_token = ?", (token,)).fetchone()
    if not user:
        abort(404)
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    user = current_user()
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    fav_only = request.args.get("fav") == "1"
    cont_only = request.args.get("continue") == "1"

    where = ["is_deleted = 0"]
    params = []

    if q:
        for term in q.split():
            like = f"%{term}%"
            where.append("(title LIKE ? OR category LIKE ? OR series LIKE ? OR filename LIKE ? OR source_url LIKE ? OR genre LIKE ? OR actors LIKE ? OR tags LIKE ?)")
            params.extend([like, like, like, like, like, like, like, like])

    if category:
        where.append("category = ?")
        params.append(category)

    if user["role"] == "viewer":
        allowed = (user["allowed_categories"] or "").strip()
        if allowed:
            cats = [x.strip() for x in allowed.split(",") if x.strip()]
            placeholders = ",".join(["?"] * len(cats))
            where.append(f"category IN ({placeholders})")
            params.extend(cats)
        where.append("age_rating <= ?")
        params.append(int(user["min_age"] or 0))

    sql = "SELECT * FROM videos WHERE " + " AND ".join(where)
    sql += " ORDER BY category COLLATE NOCASE, series COLLATE NOCASE, season, episode, id DESC"

    with db() as con:
        videos = con.execute(sql, params).fetchall()
        favorites = set([r["video_id"] for r in con.execute("SELECT video_id FROM favorites WHERE user_id = ?", (user["id"],)).fetchall()])
        history = {r["video_id"]: r for r in con.execute("SELECT * FROM watch_history WHERE user_id = ?", (user["id"],)).fetchall()}
        categories = con.execute("SELECT category, COUNT(*) AS count FROM videos WHERE is_deleted = 0 GROUP BY category ORDER BY category COLLATE NOCASE").fetchall()

    if fav_only:
        videos = [v for v in videos if v["id"] in favorites]
    if cont_only:
        videos = [v for v in videos if v["id"] in history and float(history[v["id"]]["position"] or 0) > 10]

    grouped = {}
    for v in videos:
        grouped.setdefault(v["category"] or "Filme", []).append(v)

    return render_template("index.html", grouped=grouped, categories=categories, q=q, selected_category=category, favorites=favorites, history=history)


@app.route("/series")
@login_required
def series_page():
    user = current_user()
    where = ["is_deleted = 0", "series IS NOT NULL", "series != ''"]
    params = []
    if user["role"] == "viewer":
        where.append("age_rating <= ?")
        params.append(int(user["min_age"] or 0))
    with db() as con:
        rows = con.execute("SELECT * FROM videos WHERE " + " AND ".join(where) + " ORDER BY series, season, episode", params).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r["series"], {}).setdefault(r["season"] or 0, []).append(r)
    return render_template("series.html", grouped=grouped)


@app.route("/queue")
@login_required
@role_required("admin", "downloader")
def queue_page():
    with db() as con:
        jobs = con.execute("""
            SELECT j.*, u.username AS username
            FROM download_jobs j
            LEFT JOIN users u ON u.id = j.created_by
            WHERE j.status != 'done'
            ORDER BY
                CASE j.status
                    WHEN 'running' THEN 1
                    WHEN 'queued' THEN 2
                    WHEN 'error' THEN 3
                    WHEN 'cancelled' THEN 4
                    WHEN 'done' THEN 5
                    ELSE 6
                END,
                j.priority ASC, j.created_at ASC
        """).fetchall()
        waiting_count = con.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE status = 'queued'").fetchone()["c"]
        running_count = con.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE status = 'running'").fetchone()["c"]
    return render_template("queue.html", jobs=jobs, waiting_count=waiting_count, running_count=running_count, paused=PAUSED, cookie_exists=COOKIE_FILE.exists(), schedule=DOWNLOAD_SCHEDULE_ENABLED, start=DOWNLOAD_WINDOW_START, end=DOWNLOAD_WINDOW_END)


@app.route("/api/download", methods=["POST"])
@login_required
@role_required("admin", "downloader")
def api_download():
    user = current_user()
    data = request.get_json(silent=True) or {}
    raw = data.get("urls") or data.get("url") or ""
    urls_raw = [u.strip() for u in str(raw).splitlines() if u.strip()]
    created, skipped = enqueue_urls(
        urls_raw, user["id"],
        (data.get("category") or "Filme").strip(),
        (data.get("series") or "").strip(),
        data.get("season") or "",
        data.get("episode") or "",
        data.get("age_rating") or 0,
        data.get("priority") or 100,
        data.get("media_type") or "video",
        bool(data.get("playlist"))
    )
    return jsonify({"created": created, "skipped": skipped, "created_count": len(created), "skipped_count": len(skipped)})


@app.route("/queue/retry/<job_id>", methods=["POST"])
@login_required
@role_required("admin", "downloader")
def retry_job(job_id):
    ensure_worker()
    with db() as con:
        j = con.execute("SELECT * FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
        if not j or j["status"] not in ("error", "cancelled"):
            abort(404)
        con.execute("UPDATE download_jobs SET status = ?, progress = ?, message = ?, started_at = NULL, finished_at = NULL WHERE id = ?",
                    ("queued", "0%", "Erneut in Warteschlange", job_id))
    DOWNLOAD_QUEUE.put({
        "job_id": j["id"],
        "url": j["url"],
        "category": j["category"] or "Filme",
        "series": j["series"] or "",
        "season": j["season"] or "",
        "episode": j["episode"] or "",
        "age_rating": j["age_rating"] or 0,
        "created_by": j["created_by"],
        "media_type": j["media_type"] or "video",
        "playlist": bool(j["playlist"] or 0),
    })
    save_queue_file()
    return redirect(url_for("queue_page"))


@app.route("/queue/cancel/<job_id>", methods=["POST"])
@login_required
@role_required("admin", "downloader")
def cancel_job(job_id):
    CANCELLED.add(job_id)
    with db() as con:
        con.execute("UPDATE download_jobs SET status = ?, message = ?, finished_at = ? WHERE id = ?",
                    ("cancelled", "Abgebrochen", datetime.utcnow().isoformat(timespec="seconds") + "Z", job_id))
    set_job(job_id, status="cancelled", message="Abgebrochen")
    save_queue_file()
    return redirect(url_for("queue_page"))


@app.route("/queue/delete/<job_id>", methods=["POST"])
@login_required
@role_required("admin")
def delete_job(job_id):
    with db() as con:
        con.execute("DELETE FROM download_jobs WHERE id = ?", (job_id,))
    save_queue_file()
    return redirect(url_for("queue_page"))


@app.route("/queue/pause", methods=["POST"])
@login_required
@role_required("admin")
def pause_queue():
    global PAUSED
    PAUSED = True
    return redirect(url_for("queue_page"))


@app.route("/queue/resume", methods=["POST"])
@login_required
@role_required("admin")
def resume_queue():
    global PAUSED
    PAUSED = False
    return redirect(url_for("queue_page"))


@app.route("/watch/<int:video_id>")
@login_required
def watch(video_id):
    user = current_user()
    with db() as con:
        video = con.execute("SELECT * FROM videos WHERE id = ? AND is_deleted = 0", (video_id,)).fetchone()
        fav = con.execute("SELECT 1 FROM favorites WHERE user_id = ? AND video_id = ?", (user["id"], video_id)).fetchone()
        hist = con.execute("SELECT * FROM watch_history WHERE user_id = ? AND video_id = ?", (user["id"], video_id)).fetchone()
        next_episode = None
        if video and video["series"]:
            next_episode = con.execute("""
                SELECT * FROM videos
                WHERE is_deleted = 0 AND series = ?
                AND (
                    season > ?
                    OR (season = ? AND episode > ?)
                )
                ORDER BY season ASC, episode ASC
                LIMIT 1
            """, (video["series"], video["season"] or 0, video["season"] or 0, video["episode"] or 0)).fetchone()
    if not video:
        abort(404)
    if user["role"] == "viewer" and (not category_allowed(user, video["category"]) or int(video["age_rating"] or 0) > int(user["min_age"] or 0)):
        abort(403)
    return render_template("watch.html", video=video, fav=bool(fav), hist=hist, next_episode=next_episode)


@app.route("/api/watch/<int:video_id>", methods=["POST"])
@login_required
def save_watch(video_id):
    user = current_user()
    data = request.get_json(silent=True) or {}
    pos = float(data.get("position") or 0)
    dur = float(data.get("duration") or 0)
    with db() as con:
        con.execute("""
            INSERT INTO watch_history(user_id, video_id, position, duration, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, video_id) DO UPDATE SET
                position = excluded.position,
                duration = excluded.duration,
                updated_at = excluded.updated_at
        """, (user["id"], video_id, pos, dur, datetime.utcnow().isoformat(timespec="seconds") + "Z"))
    return jsonify({"ok": True})


@app.route("/favorite/<int:video_id>", methods=["POST"])
@login_required
def toggle_favorite(video_id):
    user = current_user()
    with db() as con:
        exists = con.execute("SELECT 1 FROM favorites WHERE user_id = ? AND video_id = ?", (user["id"], video_id)).fetchone()
        if exists:
            con.execute("DELETE FROM favorites WHERE user_id = ? AND video_id = ?", (user["id"], video_id))
        else:
            con.execute("INSERT OR IGNORE INTO favorites(user_id, video_id, created_at) VALUES (?, ?, ?)",
                        (user["id"], video_id, datetime.utcnow().isoformat(timespec="seconds") + "Z"))
    return redirect(request.referrer or url_for("index"))


@app.route("/delete/<int:video_id>", methods=["POST"])
@login_required
@role_required("admin")
def delete_video(video_id):
    with db() as con:
        video = con.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not video:
            abort(404)
        try:
            src = DOWNLOAD_DIR / video["filename"]
            if src.exists():
                shutil.move(str(src), str(TRASH_DIR / video["filename"]))
            for col, folder in [("thumbnail", THUMB_DIR), ("poster", POSTER_DIR), ("subtitle", SUBTITLE_DIR)]:
                if video[col]:
                    f = folder / video[col]
                    if f.exists():
                        shutil.move(str(f), str(TRASH_DIR / video[col]))
        except Exception:
            pass
        con.execute("UPDATE videos SET is_deleted = 1, deleted_at = ? WHERE id = ?",
                    (datetime.utcnow().isoformat(timespec="seconds") + "Z", video_id))
    dlna_marker()
    return redirect(url_for("index"))


@app.route("/trash")
@login_required
@role_required("admin")
def trash():
    with db() as con:
        videos = con.execute("SELECT * FROM videos WHERE is_deleted = 1 ORDER BY deleted_at DESC").fetchall()
    return render_template("trash.html", videos=videos)


@app.route("/trash/restore/<int:video_id>", methods=["POST"])
@login_required
@role_required("admin")
def restore_video(video_id):
    with db() as con:
        v = con.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not v:
            abort(404)
        try:
            src = TRASH_DIR / v["filename"]
            if src.exists():
                shutil.move(str(src), str(DOWNLOAD_DIR / v["filename"]))
        except Exception:
            pass
        con.execute("UPDATE videos SET is_deleted = 0, deleted_at = NULL WHERE id = ?", (video_id,))
    dlna_marker()
    return redirect(url_for("trash"))


@app.route("/trash/delete/<int:video_id>", methods=["POST"])
@login_required
@role_required("admin")
def mark_video_for_purge(video_id):
    with db() as con:
        exists = con.execute("SELECT id FROM videos WHERE id = ? AND is_deleted = 1", (video_id,)).fetchone()
        if not exists:
            abort(404)

        con.execute("""
            UPDATE videos
            SET delete_pending = 1,
                delete_pending_at = ?
            WHERE id = ?
        """, (
            datetime.utcnow().isoformat(timespec="seconds") + "Z",
            video_id
        ))

    flash("Zur nächtlichen endgültigen Löschung vorgemerkt.")
    return redirect(url_for("trash"))


@app.route("/trash/unmark/<int:video_id>", methods=["POST"])
@login_required
@role_required("admin")
def unmark_video_for_purge(video_id):
    with db() as con:
        con.execute("""
            UPDATE videos
            SET delete_pending = 0,
                delete_pending_at = NULL
            WHERE id = ?
        """, (video_id,))

    flash("Loeschvormerkung wurde zurueckgenommen.")
    return redirect(url_for("trash"))


@app.route("/cron/purge-trash")
def cron_purge_trash():
    token = request.args.get("token", "")
    expected = os.environ.get("CRON_TOKEN", "")

    if not expected or token != expected:
        abort(403)

    deleted = 0

    with db() as con:
        rows = con.execute("""
            SELECT * FROM videos
            WHERE is_deleted = 1
              AND delete_pending = 1
        """).fetchall()

        for v in rows:
            files = []

            if v["filename"]:
                if v["filename"].startswith("music/"):
                    files.append(MUSIC_DIR / v["filename"].replace("music/", "", 1))
                    files.append(TRASH_DIR / Path(v["filename"]).name)
                else:
                    files.append(DOWNLOAD_DIR / v["filename"])
                    files.append(TRASH_DIR / v["filename"])

            for col, folder in [
                ("thumbnail", THUMB_DIR),
                ("poster", POSTER_DIR),
                ("subtitle", SUBTITLE_DIR),
            ]:
                try:
                    if v[col]:
                        files.append(folder / v[col])
                        files.append(TRASH_DIR / v[col])
                except Exception:
                    pass

            for f in files:
                try:
                    Path(f).unlink(missing_ok=True)
                except Exception:
                    pass

            con.execute("DELETE FROM videos WHERE id = ?", (v["id"],))
            con.execute("DELETE FROM favorites WHERE video_id = ?", (v["id"],))
            con.execute("DELETE FROM watch_history WHERE video_id = ?", (v["id"],))

            deleted += 1

    dlna_marker()

    return jsonify({"ok": True, "deleted": deleted})


@app.route("/admin/video/<int:video_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def edit_video(video_id):
    with db() as con:
        video = con.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not video:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip() or video["title"]
        category = request.form.get("category", "").strip() or "Filme"
        series = request.form.get("series", "").strip()
        season = request.form.get("season", "")
        episode = request.form.get("episode", "")
        age_rating = request.form.get("age_rating", "0")
        description = request.form.get("description", "").strip()
        tags = request.form.get("tags", "").strip()
        year = request.form.get("year", "").strip()
        genre = request.form.get("genre", "").strip()
        actors = request.form.get("actors", "").strip()

        with db() as con:
            con.execute("""
                UPDATE videos
                SET title = ?, category = ?, series = ?, season = ?, episode = ?,
                    age_rating = ?, description = ?, tags = ?, year = ?, genre = ?, actors = ?
                WHERE id = ?
            """, (
                title, category, series or None,
                int(season) if str(season).isdigit() else None,
                int(episode) if str(episode).isdigit() else None,
                int(age_rating) if str(age_rating).isdigit() else 0,
                description, tags, year, genre, actors, video_id
            ))
        flash("Video gespeichert")
        return redirect(url_for("watch", video_id=video_id))

    return render_template("edit_video.html", video=video)


@app.route("/admin/video/<int:video_id>/metadata", methods=["POST"])
@login_required
@role_required("admin")
def refresh_metadata(video_id):
    with db() as con:
        video = con.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not video:
        abort(404)
    meta = enrich_metadata(video["title"])
    if meta:
        with db() as con:
            con.execute("""
                UPDATE videos SET description = ?, year = ?, genre = ?, actors = ?, tmdb_id = ?, imdb_id = ?,
                    poster = COALESCE(NULLIF(?, ''), poster)
                WHERE id = ?
            """, (
                meta.get("description") or video["description"],
                meta.get("year") or video["year"],
                meta.get("genre") or video["genre"],
                meta.get("actors") or video["actors"],
                meta.get("tmdb_id") or video["tmdb_id"],
                meta.get("imdb_id") or video["imdb_id"],
                meta.get("poster") or "",
                video_id
            ))
        flash("Metadaten aktualisiert")
    else:
        flash("Keine Metadaten gefunden oder API-Key fehlt")
    return redirect(url_for("edit_video", video_id=video_id))


@app.route("/admin/users")
@login_required
@role_required("admin")
def admin_users():
    with db() as con:
        users = con.execute("SELECT * FROM users ORDER BY id ASC").fetchall()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/create", methods=["POST"])
@login_required
@role_required("admin")
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "viewer")
    allowed_categories = request.form.get("allowed_categories", "").strip()
    min_age = request.form.get("min_age", "0")
    theme = request.form.get("theme", "netflix")
    if theme not in AVAILABLE_THEMES:
        theme = "netflix"
    if not username or not password or role not in ("viewer", "downloader", "admin"):
        flash("Ungueltige Eingabe")
        return redirect(url_for("admin_users"))
    with db() as con:
        try:
            con.execute("""
                INSERT INTO users(username, password_hash, role, allowed_categories, min_age, theme, login_token, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                username, generate_password_hash(password), role, allowed_categories,
                int(min_age) if str(min_age).isdigit() else 0, theme, uuid.uuid4().hex,
                datetime.utcnow().isoformat(timespec="seconds") + "Z"
            ))
        except sqlite3.IntegrityError:
            flash("Benutzer existiert bereits")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/update/<int:user_id>", methods=["POST"])
@login_required
@role_required("admin")
def admin_update_user(user_id):
    role = request.form.get("role", "viewer")
    allowed_categories = request.form.get("allowed_categories", "").strip()
    min_age = request.form.get("min_age", "0")
    password = request.form.get("password", "")
    theme = request.form.get("theme", "netflix")
    if theme not in AVAILABLE_THEMES:
        theme = "netflix"
    with db() as con:
        if password:
            con.execute("""
                UPDATE users SET role = ?, allowed_categories = ?, min_age = ?, theme = ?, password_hash = ?
                WHERE id = ?
            """, (role, allowed_categories, int(min_age) if str(min_age).isdigit() else 0, theme, generate_password_hash(password), user_id))
        else:
            con.execute("""
                UPDATE users SET role = ?, allowed_categories = ?, min_age = ?, theme = ?
                WHERE id = ?
            """, (role, allowed_categories, int(min_age) if str(min_age).isdigit() else 0, theme, user_id))
    return redirect(url_for("admin_users"))


@app.route("/admin/users/delete/<int:user_id>", methods=["POST"])
@login_required
@role_required("admin")
def admin_delete_user(user_id):
    if user_id == session.get("user_id"):
        flash("Du kannst dich nicht selbst löschen")
        return redirect(url_for("admin_users"))
    with db() as con:
        con.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return redirect(url_for("admin_users"))


@app.route("/settings/password", methods=["GET", "POST"])
@login_required
def change_password():
    user = current_user()
    if request.method == "POST":
        old = request.form.get("old_password", "")
        new = request.form.get("new_password", "")
        if not check_password_hash(user["password_hash"], old):
            return render_template("change_password.html", error="Altes Passwort falsch")
        if len(new) < 4:
            return render_template("change_password.html", error="Neues Passwort zu kurz")
        with db() as con:
            con.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new), user["id"]))
        flash("Passwort geändert")
        return redirect(url_for("index"))
    return render_template("change_password.html", error=None)


@app.route("/settings/theme", methods=["GET", "POST"])
@login_required
def change_theme():
    user = current_user()
    if request.method == "POST":
        theme = request.form.get("theme", "netflix")
        if theme not in AVAILABLE_THEMES:
            theme = "netflix"
        with db() as con:
            con.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user["id"]))
        flash("Design gespeichert")
        return redirect(url_for("change_theme"))
    return render_template("change_theme.html", user=user, themes=AVAILABLE_THEMES)


@app.route("/settings/qr")
@login_required
def qr_login_page():
    user = current_user()
    base = request.url_root.rstrip("/")
    login_url = f"{base}/qr-login/{user['login_token']}"
    qr_src = "https://api.qrserver.com/v1/create-qr-code/?" + urllib.parse.urlencode({"size": "260x260", "data": login_url})
    return render_template("qr_login.html", login_url=login_url, qr_src=qr_src)



@app.route("/admin/bookmarklet")
@login_required
@role_required("admin", "downloader")
def bookmarklet():
    base = request.url_root.rstrip("/")
    js = (
        "javascript:(()=>{"
        f"window.open('{base}/add-url?url='+encodeURIComponent(location.href),"
        "'streambox','width=620,height=620');"
        "})();"
    )
    return render_template("bookmarklet.html", js=js)


@app.route("/add-url", methods=["GET", "POST"])
@login_required
@role_required("admin", "downloader")
def add_url_page():
    user = current_user()
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        category = request.form.get("category", "Bookmarklet").strip() or "Bookmarklet"
        media_type = request.form.get("media_type", "video")
        playlist = request.form.get("playlist") == "1"

        created, skipped = enqueue_urls(
            [url], user["id"], category, "", "", "", 0, 100, media_type, playlist
        )
        return render_template("add_url.html", url=url, categories=[], sent=True, created=created, skipped=skipped)

    url = request.args.get("url", "").strip()
    with db() as con:
        categories = con.execute("""
            SELECT category, COUNT(*) AS count
            FROM videos
            WHERE is_deleted = 0
            GROUP BY category
            ORDER BY category COLLATE NOCASE
        """).fetchall()

    return render_template("add_url.html", url=url, categories=categories, sent=False, created=[], skipped=[])



@app.route("/admin/domains")
@login_required
@role_required("admin")
def admin_domains():
    with db() as con:
        domains = con.execute("SELECT * FROM allowed_domains ORDER BY enabled DESC, domain ASC").fetchall()
    return render_template("admin_domains.html", domains=domains)


@app.route("/admin/domains/create", methods=["POST"])
@login_required
@role_required("admin")
def admin_domains_create():
    domain = request.form.get("domain", "").strip().lower()
    note = request.form.get("note", "").strip()
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].strip()
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or "." not in domain:
        flash("Ungueltige Domain")
        return redirect(url_for("admin_domains"))
    with db() as con:
        try:
            con.execute("""
                INSERT INTO allowed_domains(domain, note, enabled, created_at)
                VALUES (?, ?, 1, ?)
            """, (domain, note, datetime.utcnow().isoformat(timespec="seconds") + "Z"))
            flash("Domain hinzugefuegt")
        except sqlite3.IntegrityError:
            flash("Domain existiert bereits")
    return redirect(url_for("admin_domains"))


@app.route("/admin/domains/update/<int:domain_id>", methods=["POST"])
@login_required
@role_required("admin")
def admin_domains_update(domain_id):
    domain = request.form.get("domain", "").strip().lower()
    note = request.form.get("note", "").strip()
    enabled = 1 if request.form.get("enabled") == "1" else 0
    domain = domain.replace("https://", "").replace("http://", "").split("/")[0].strip()
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain or "." not in domain:
        flash("Ungueltige Domain")
        return redirect(url_for("admin_domains"))
    with db() as con:
        con.execute("UPDATE allowed_domains SET domain = ?, note = ?, enabled = ? WHERE id = ?", (domain, note, enabled, domain_id))
    return redirect(url_for("admin_domains"))


@app.route("/admin/domains/delete/<int:domain_id>", methods=["POST"])
@login_required
@role_required("admin")
def admin_domains_delete(domain_id):
    with db() as con:
        con.execute("DELETE FROM allowed_domains WHERE id = ?", (domain_id,))
    return redirect(url_for("admin_domains"))


def read_env_file():
    env_path = Path("/srv/streambox/.env")
    values = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


def pi_temperature():
    try:
        p = Path("/sys/class/thermal/thermal_zone0/temp")
        if p.exists():
            return round(int(p.read_text().strip()) / 1000, 1)
    except Exception:
        pass
    return None


def load_average():
    try:
        return os.getloadavg()
    except Exception:
        return None


def folder_size(path):
    total = 0
    try:
        for p in Path(path).rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except Exception:
        pass
    return total



@app.route("/admin/migrate-db", methods=["POST"])
@login_required
@role_required("admin")
def admin_migrate_db():
    init_db()
    flash("Datenbank-Migration ausgeführt.")
    return redirect(url_for("admin_status"))


@app.route("/admin/updates")
@login_required
@role_required("admin")
def admin_updates():
    status = git_update_status()
    log_dir = DATA_DIR / "update_logs"
    logs = []
    try:
        logs = sorted(log_dir.glob("update_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    except Exception:
        logs = []
    return render_template("admin_updates.html", status=status, logs=logs)


@app.route("/admin/updates/run", methods=["POST"])
@login_required
@role_required("admin")
def admin_updates_run():
    script = BASE_DIR / "update_from_github.sh"
    if not script.exists():
        flash("Update-Script fehlt: update_from_github.sh")
        return redirect(url_for("admin_updates"))

    subprocess.Popen(
        ["bash", str(script)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    flash("Update wurde gestartet. Bitte 1-5 Minuten warten und dann die Seite neu laden.")
    return redirect(url_for("admin_updates"))


@app.route("/admin/rebuild", methods=["POST"])
@login_required
@role_required("admin")
def admin_rebuild():
    script = BASE_DIR / "rebuild.sh"
    if not script.exists():
        flash("Rebuild-Script fehlt: rebuild.sh")
        return redirect(url_for("admin_updates"))

    subprocess.Popen(
        ["bash", str(script)],
        cwd=str(BASE_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    flash("Rebuild wurde gestartet. Bitte kurz warten.")
    return redirect(url_for("admin_updates"))


@app.route("/admin/update-log/<path:name>")
@login_required
@role_required("admin")
def admin_update_log(name):
    safe = Path(name).name
    log_file = DATA_DIR / "update_logs" / safe
    if not log_file.exists():
        abort(404)
    return "<pre>" + log_file.read_text(encoding="utf-8", errors="replace")[-20000:] + "</pre>"


@app.route("/admin/status")
@login_required
@role_required("admin")
def admin_status():
    usage = shutil.disk_usage(str(DATA_DIR))
    env = read_env_file()
    marker = DATA_DIR / "dlna_rescan_requested.txt"
    with db() as con:
        queued = con.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE status = 'queued'").fetchone()["c"]
        running = con.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE status = 'running'").fetchone()["c"]
        errors = con.execute("SELECT COUNT(*) AS c FROM download_jobs WHERE status = 'error'").fetchone()["c"]
        videos = con.execute("SELECT COUNT(*) AS c FROM videos WHERE is_deleted = 0").fetchone()["c"]
    return render_template("admin_status.html", usage=usage, env=env, queued=queued, running=running, errors=errors, videos=videos, marker=marker.exists(), cookie_exists=COOKIE_FILE.exists(), temp=pi_temperature(), load=load_average(), media_size=folder_size(DOWNLOAD_DIR), platform_name=platform.platform(), telegram=bool(TELEGRAM_BOT_TOKEN), schedule=DOWNLOAD_SCHEDULE_ENABLED, start=DOWNLOAD_WINDOW_START, end=DOWNLOAD_WINDOW_END, transcode=TRANSCODE_PS4, tmdb=bool(TMDB_API_KEY), omdb=bool(OMDB_API_KEY))


@app.route("/admin/dlna-marker", methods=["POST"])
@login_required
@role_required("admin")
def admin_dlna_marker():
    dlna_marker()
    flash("DLNA-Rescan markiert. Starte MiniDLNA neu: sudo docker restart minidlna")
    return redirect(url_for("admin_status"))


@app.route("/admin/update-ytdlp", methods=["POST"])
@login_required
@role_required("admin")
def admin_update_ytdlp():
    try:
        subprocess.run("python -m pip install -U 'yt-dlp[default,curl-cffi]'", shell=True, timeout=300)
        flash("yt-dlp Update ausgeführt. Container-Neustart empfohlen.")
    except Exception as e:
        flash(str(e))
    return redirect(url_for("admin_status"))


@app.route("/admin/backup", methods=["POST"])
@login_required
@role_required("admin")
def create_backup():
    name = "streambox_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / name
    dest.mkdir(exist_ok=True)
    shutil.copy2(DB_PATH, dest / "media.db")
    shutil.copy2(Path("/srv/streambox/.env"), dest / ".env") if Path("/srv/streambox/.env").exists() else None
    shutil.copy2(QUEUE_FILE, dest / "queue.txt") if QUEUE_FILE.exists() else None
    flash(f"Backup erstellt: {name}")
    return redirect(url_for("admin_status"))


@app.route("/random")
@login_required
def random_video():
    user = current_user()
    where = ["is_deleted = 0"]
    params = []
    if user["role"] == "viewer":
        where.append("age_rating <= ?")
        params.append(int(user["min_age"] or 0))
    with db() as con:
        rows = con.execute("SELECT id FROM videos WHERE " + " AND ".join(where), params).fetchall()
    if not rows:
        return redirect(url_for("index"))
    return redirect(url_for("watch", video_id=random.choice(rows)["id"]))


@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(Path(__file__).resolve().parent / "static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory(Path(__file__).resolve().parent / "static", "service-worker.js", mimetype="application/javascript")


@app.route("/media/<path:filename>")
@login_required
def media(filename):
    if filename.startswith("music/"):
        return send_from_directory(MUSIC_DIR, filename.replace("music/", "", 1), as_attachment=False, conditional=True)
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=False, conditional=True)


@app.route("/posters/<path:filename>")
@login_required
def posters(filename):
    return send_from_directory(POSTER_DIR, filename, as_attachment=False)


@app.route("/thumbs/<path:filename>")
@login_required
def thumbs(filename):
    return send_from_directory(THUMB_DIR, filename, as_attachment=False)


@app.route("/subs/<path:filename>")
@login_required
def subs(filename):
    return send_from_directory(SUBTITLE_DIR, filename, as_attachment=False)


if __name__ == "__main__":
    init_db()
    restore_queue_from_db()
    start_telegram()
    socketio.run(app, host="0.0.0.0", port=8080, debug=False, allow_unsafe_werkzeug=True)
