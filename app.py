#!/usr/bin/env python3
import cmath
import json
import os
import random
import re
import shutil
import struct
import socket
import sqlite3
import subprocess
import sys
import datetime
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from flask import Flask, g, jsonify, request, send_file, abort, send_from_directory, make_response, Response

import config as jukebox_config

MUSIC_DIR = jukebox_config.get_music_dir()
DEFAULT_DB_PATH = os.path.join(jukebox_config.get_app_data_dir(), "library.db")
DB_PATH = os.environ.get("JUKEBOX_DB_PATH", DEFAULT_DB_PATH)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

ART_CACHE_DIR = os.path.join(os.path.dirname(DB_PATH), "art_cache")
os.makedirs(ART_CACHE_DIR, exist_ok=True)

TRASH_DIR = os.path.join(os.path.dirname(DB_PATH), "trash")
BACKUP_DIR = os.path.join(os.path.dirname(DB_PATH), "backups")

STATIC_DIR = os.environ.get(
    "JUKEBOX_STATIC_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="")


@app.after_request
def _no_cache_app_shell(response):
    # index.html/app.js/style.css get re-synced in place across updates,
    # with nothing in the URL changing to bust a cache -- pywebview's
    # WKWebView has been observed serving a stale cached copy of these
    # after a sync even when a plain browser tab correctly picks up the
    # new version, so force no caching at all rather than rely on
    # conditional revalidation (ETag/Last-Modified) actually happening.
    # Doesn't touch other responses, like art images, that set their own
    # deliberate long-lived Cache-Control elsewhere.
    if request.path in ("/", "/index.html", "/app.js", "/style.css"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


_schema_ready = False


def _ensure_deep_scan_columns(db):
    """Cache columns for the tag-checker's deep scan (raw artist/title/art
    presence read straight from each file) -- added lazily so existing
    databases from before this feature don't need a manual migration."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(tracks)").fetchall()}
    if "has_artist_tag" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN has_artist_tag INTEGER")
    if "has_title_tag" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN has_title_tag INTEGER")
    if "has_art" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN has_art INTEGER")
    if "play_count" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN play_count INTEGER NOT NULL DEFAULT 0")
    if "last_played_at" not in cols:
        db.execute("ALTER TABLE tracks ADD COLUMN last_played_at TEXT")
    db.execute("""
        CREATE TABLE IF NOT EXISTS trash (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_path TEXT NOT NULL,
            trash_path TEXT NOT NULL,
            artist TEXT, title TEXT, album TEXT,
            trashed_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS smart_playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            rules TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # Full-text index for search, kept in sync automatically by triggers so
    # every write path (scan_library's raw sqlite3 connection included)
    # stays covered without having to remember to update it everywhere.
    try:
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
                artist, title, album, content='tracks', content_rowid='id'
            )
        """)
        # `SELECT COUNT(*) FROM tracks_fts` is not a reliable staleness check
        # on every SQLite/FTS5 build -- on 3.51.0 it reports the content
        # table's row count even when the index's own shadow tables are
        # still completely empty (verified: right after CREATE VIRTUAL
        # TABLE, before any rebuild has run). That silently skips the very
        # rebuild it exists to trigger, leaving a present-but-unpopulated
        # index, and every subsequent trigger-driven write on it then fails
        # with "database disk image is malformed" even though PRAGMA
        # integrity_check and FTS5's own integrity-check both report clean.
        # tracks_fts_docsize holds one row per actually-indexed document, so
        # count that instead -- it always exists once tracks_fts does.
        indexed = db.execute("SELECT COUNT(*) FROM tracks_fts_docsize").fetchone()[0]
        total = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        if indexed != total:
            db.execute("INSERT INTO tracks_fts(tracks_fts) VALUES ('rebuild')")
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS tracks_fts_ai AFTER INSERT ON tracks BEGIN
                INSERT INTO tracks_fts(rowid, artist, title, album) VALUES (new.id, new.artist, new.title, new.album);
            END
        """)
        db.execute("""
            CREATE TRIGGER IF NOT EXISTS tracks_fts_ad AFTER DELETE ON tracks BEGIN
                INSERT INTO tracks_fts(tracks_fts, rowid, artist, title, album) VALUES ('delete', old.id, old.artist, old.title, old.album);
            END
        """)
        # WHEN guard: without it this fires (and rewrites the FTS index) on
        # every update to the tracks row -- including play_count/last_played_at
        # bumps on every play and has_art/path housekeeping writes that never
        # touch artist/title/album at all. Dropped and recreated (rather than
        # IF NOT EXISTS) so an existing library.db from before this guard
        # existed actually picks it up instead of keeping its old, unguarded
        # trigger forever.
        db.execute("DROP TRIGGER IF EXISTS tracks_fts_au")
        db.execute("""
            CREATE TRIGGER tracks_fts_au AFTER UPDATE ON tracks
            WHEN old.artist IS NOT new.artist OR old.title IS NOT new.title OR old.album IS NOT new.album
            BEGIN
                INSERT INTO tracks_fts(tracks_fts, rowid, artist, title, album) VALUES ('delete', old.id, old.artist, old.title, old.album);
                INSERT INTO tracks_fts(rowid, artist, title, album) VALUES (new.id, new.artist, new.title, new.album);
            END
        """)
    except sqlite3.OperationalError:
        pass  # FTS5 not compiled into this SQLite build -- search just falls back to LIKE
    db.commit()


def get_db():
    global _schema_ready
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    if not _schema_ready:
        _ensure_deep_scan_columns(g.db)
        _schema_ready = True
    return g.db


def _snapshot_db():
    """Copies the whole library.db before a bulk destructive action, keeping
    the last 5 snapshots. Cheap (it's one file) and turns "the auto-clean
    did something unexpected" into a non-event instead of a real loss."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"library-{ts}.db")
        shutil.copy2(DB_PATH, dest)
        snapshots = sorted(
            f for f in os.listdir(BACKUP_DIR) if f.startswith("library-") and f.endswith(".db")
        )
        for old in snapshots[:-5]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except OSError:
        pass


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def tempo_bucket(bpm):
    if bpm is None:
        return None
    if bpm < 90:
        return "slow"
    if bpm < 130:
        return "mid"
    return "fast"


def track_to_dict(row, rating=None):
    d = dict(row)
    d["tempo_bucket"] = tempo_bucket(d.get("bpm"))
    if rating is not None:
        d["rating"] = rating
    return d


TRACK_FIELDS = (
    "t.id, t.artist, t.album, t.title, t.genre, t.primary_genre, t.year, "
    "t.decade, t.bpm, t.duration, t.ext, t.language, r.rating as rating"
)


# ---------------------------------------------------------------- browsing --

# Shared by /api/rescan and /api/choose-folder -- both ultimately just run
# scan_library.scan() over MUSIC_DIR, the only difference being whether the
# folder itself changed first. A full scan of a large library (tens of
# thousands of files, as a freshly-connected drive full of lossless FLACs
# can easily be) took minutes with the old synchronous-request version of
# this, during which the UI had no way to distinguish "still working" from
# "hung" -- there was no progress endpoint to poll, unlike fill-genres/
# fill-years/duplicate-cleanup, which already use this exact pattern.
_scan_state = {"running": False, "done": 0, "total": 0, "result": None, "error": None}
_scan_lock = threading.Lock()


def _run_scan_bg():
    def progress_cb(done, total):
        _scan_state["done"] = done
        _scan_state["total"] = total

    try:
        import importlib
        import scan_library
        importlib.reload(scan_library)
        stats = scan_library.scan(progress_cb=progress_cb)
        _scan_state["result"] = stats
    except Exception as e:
        _scan_state["error"] = str(e)
    finally:
        _scan_state["running"] = False


def _start_scan_bg():
    """Returns False (and starts nothing) if a scan is already running --
    same "already running" convention as fill-genres/fill-years, not an
    error, just something the caller can tell the user."""
    with _scan_lock:
        if _scan_state["running"]:
            return False
        _scan_state.update(running=True, done=0, total=0, result=None, error=None)
        close_db(None)
        threading.Thread(target=_run_scan_bg, daemon=True).start()
        return True


@app.route("/api/rescan", methods=["POST"])
def rescan():
    _snapshot_db()
    started = _start_scan_bg()
    return jsonify({"started": started, "error": None if started else "Already running"})


@app.route("/api/scan-progress")
def scan_progress():
    return jsonify(_scan_state)


# Same async-job-with-progress-polling shape as the scan above: moving
# thousands of files into per-artist folders is exactly the kind of thing
# that can take minutes and must never block a request while it runs.
_organize_state = {"running": False, "done": 0, "total": 0, "moved": 0, "result": None, "error": None}
_organize_lock = threading.Lock()


def _run_organize_bg():
    def progress_cb(done, total, moved):
        _organize_state["done"] = done
        _organize_state["total"] = total
        _organize_state["moved"] = moved

    try:
        import importlib
        import organize_by_artist
        importlib.reload(organize_by_artist)
        stats = organize_by_artist.organize(progress_cb=progress_cb)

        # Files physically moved -- repoint the existing tracks.path rows at
        # their new location instead of leaving the DB pointing at paths
        # that no longer exist. Done by path (not a full rescan) so track
        # ids, ratings, and playlist membership all survive the move
        # untouched; a rescan here would instead delete+reinsert every
        # moved track under a new id and silently drop its rating.
        path_moves = stats.pop("path_moves", {})
        if path_moves:
            # The files are already moved on disk at this point -- losing
            # this update would leave the DB pointing at paths that no
            # longer exist (and a later rescan "fixing" that would delete +
            # reinsert every one of these tracks under a new id, dropping
            # its rating right back out). A transient sqlite lock from a
            # concurrent request is exactly the kind of failure worth
            # retrying rather than accepting silently.
            last_err = None
            for attempt in range(5):
                try:
                    conn = sqlite3.connect(DB_PATH, timeout=30)
                    conn.execute("PRAGMA busy_timeout = 30000")
                    conn.executemany(
                        "UPDATE tracks SET path=? WHERE path=?",
                        [(new, old) for old, new in path_moves.items()],
                    )
                    conn.commit()
                    conn.close()
                    last_err = None
                    break
                except sqlite3.Error as e:
                    last_err = e
                    time.sleep(0.5 * (attempt + 1))
            if last_err:
                raise RuntimeError(
                    f"Files were moved on disk, but updating the library index failed "
                    f"({last_err}). Run Rescan to re-sync the library."
                )

        _organize_state["result"] = stats
    except Exception as e:
        _organize_state["error"] = str(e)
    finally:
        _organize_state["running"] = False


def _start_organize_bg():
    with _organize_lock:
        if _organize_state["running"]:
            return False
        _organize_state.update(running=True, done=0, total=0, moved=0, result=None, error=None)
        close_db(None)
        threading.Thread(target=_run_organize_bg, daemon=True).start()
        return True


@app.route("/api/organize-by-artist", methods=["POST"])
def organize_by_artist_route():
    _snapshot_db()
    started = _start_organize_bg()
    return jsonify({"started": started, "error": None if started else "Already running"})


@app.route("/api/organize-progress")
def organize_progress():
    return jsonify(_organize_state)


_fill_genres_state = {"running": False, "done": 0, "total": 0, "found": 0, "result": None, "error": None}
_fill_genres_lock = threading.Lock()


def _run_fill_genres_bg():
    def progress_cb(done, total, found):
        _fill_genres_state["done"] = done
        _fill_genres_state["total"] = total
        _fill_genres_state["found"] = found

    try:
        import importlib
        import fill_genres
        importlib.reload(fill_genres)
        stats = fill_genres.fill_missing_genres(progress_cb=progress_cb)
        _fill_genres_state["result"] = stats
    except Exception as e:
        _fill_genres_state["error"] = str(e)
    finally:
        _fill_genres_state["running"] = False


@app.route("/api/fill-genres", methods=["POST"])
def fill_genres_route():
    # 200 either way (not a 409) -- "already running" is an expected,
    # normal outcome for the frontend to branch on, not a request failure,
    # and the shared api() helper throws on any non-2xx response.
    with _fill_genres_lock:
        if _fill_genres_state["running"]:
            return jsonify({"started": False, "error": "Already running"})
        _fill_genres_state.update(running=True, done=0, total=0, found=0, result=None, error=None)
        close_db(None)
        threading.Thread(target=_run_fill_genres_bg, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/fill-genres/progress")
def fill_genres_progress():
    return jsonify(_fill_genres_state)


_fill_years_state = {"running": False, "done": 0, "total": 0, "updated": 0, "result": None, "error": None}
_fill_years_lock = threading.Lock()


def _run_fill_years_bg(track_ids):
    def progress_cb(done, total, updated):
        _fill_years_state["done"] = done
        _fill_years_state["total"] = total
        _fill_years_state["updated"] = updated

    try:
        import importlib
        import fill_years
        importlib.reload(fill_years)
        _snapshot_db()
        stats = fill_years.fix_release_years(progress_cb=progress_cb, track_ids=track_ids)
        _fill_years_state["result"] = stats
    except Exception as e:
        _fill_years_state["error"] = str(e)
    finally:
        _fill_years_state["running"] = False


@app.route("/api/fill-years", methods=["POST"])
def fill_years_route():
    """Corrects tracks toward their original release year using Deezer.
    Pass {"track_ids": [...]} to scope it (e.g. to the current filtered
    view); omit it to run across the whole library."""
    data = request.get_json(force=True, silent=True) or {}
    track_ids = data.get("track_ids") or None
    with _fill_years_lock:
        if _fill_years_state["running"]:
            return jsonify({"started": False, "error": "Already running"})
        _fill_years_state.update(running=True, done=0, total=0, updated=0, result=None, error=None)
        close_db(None)
        threading.Thread(target=_run_fill_years_bg, args=(track_ids,), daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/fill-years/progress")
def fill_years_progress():
    return jsonify(_fill_years_state)


_unify_genre_state = {"running": False, "done": 0, "total": 0, "updated": 0, "result": None, "error": None}
_unify_genre_lock = threading.Lock()


def _run_unify_genre_bg():
    def progress_cb(done, total, updated):
        _unify_genre_state["done"] = done
        _unify_genre_state["total"] = total
        _unify_genre_state["updated"] = updated

    try:
        import importlib
        import unify_artist_genre
        importlib.reload(unify_artist_genre)
        stats = unify_artist_genre.unify_artist_genres(progress_cb=progress_cb)
        _unify_genre_state["result"] = stats
    except Exception as e:
        _unify_genre_state["error"] = str(e)
    finally:
        _unify_genre_state["running"] = False


@app.route("/api/unify-artist-genre/preview")
def unify_artist_genre_preview():
    """Fast, read-only counts (and the biggest-impact examples) for a
    confirmation prompt before the real run -- see preview_unify_artist_genres
    in unify_artist_genre.py for why this one gets a preview and
    fill-genres/fill-years don't."""
    import unify_artist_genre
    return jsonify(unify_artist_genre.preview_unify_artist_genres())


@app.route("/api/unify-artist-genre", methods=["POST"])
def unify_artist_genre_route():
    """For every artist with more than one genre across their tracks, picks
    the most common one and writes it into every track by that artist."""
    with _unify_genre_lock:
        if _unify_genre_state["running"]:
            return jsonify({"started": False, "error": "Already running"})
        _snapshot_db()
        _unify_genre_state.update(running=True, done=0, total=0, updated=0, result=None, error=None)
        close_db(None)
        threading.Thread(target=_run_unify_genre_bg, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/unify-artist-genre/progress")
def unify_artist_genre_progress():
    return jsonify(_unify_genre_state)


_fix_artist_title_state = {"running": False, "done": 0, "total": 0, "updated": 0, "result": None, "error": None}
_fix_artist_title_lock = threading.Lock()


def _run_fix_artist_title_bg():
    def progress_cb(done, total, updated):
        _fix_artist_title_state["done"] = done
        _fix_artist_title_state["total"] = total
        _fix_artist_title_state["updated"] = updated

    try:
        import importlib
        import fix_artist_title
        importlib.reload(fix_artist_title)
        stats = fix_artist_title.fix_artist_title(progress_cb=progress_cb)
        _fix_artist_title_state["result"] = stats
    except Exception as e:
        _fix_artist_title_state["error"] = str(e)
    finally:
        _fix_artist_title_state["running"] = False


@app.route("/api/fix-artist-title/preview")
def fix_artist_title_preview():
    """Fast(er) read-only estimate -- samples the library rather than
    running a full pass, since checking every track against Deezer just to
    show a preview would take as long as the real run. See
    preview_fix_artist_title in fix_artist_title.py."""
    import fix_artist_title
    return jsonify(fix_artist_title.preview_fix_artist_title())


@app.route("/api/fix-artist-title", methods=["POST"])
def fix_artist_title_route():
    """Corrects artist/title spelling and capitalization toward Deezer's
    catalog, Picard-style -- see fix_artist_title.py for exactly what is
    and isn't considered safe to auto-correct."""
    with _fix_artist_title_lock:
        if _fix_artist_title_state["running"]:
            return jsonify({"started": False, "error": "Already running"})
        _snapshot_db()
        _fix_artist_title_state.update(running=True, done=0, total=0, updated=0, result=None, error=None)
        close_db(None)
        threading.Thread(target=_run_fix_artist_title_bg, daemon=True).start()
    return jsonify({"started": True})


@app.route("/api/fix-artist-title/progress")
def fix_artist_title_progress():
    return jsonify(_fix_artist_title_state)


@app.route("/api/tracks/<int:track_id>/lookup-tags", methods=["POST"])
def lookup_track_tags(track_id):
    """Picard-style single-track refresh: runs the same Deezer-backed
    lookups the bulk tools use (missing genre, original release year,
    artist/title formatting) against just this one track. Synchronous --
    a handful of API calls for one track finishes in a couple of seconds,
    nowhere near needing the background-job treatment the whole-library
    versions get."""
    db = get_db()
    if not db.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone():
        abort(404)

    import importlib
    import fill_genres, fill_years, fix_artist_title
    importlib.reload(fill_genres)
    importlib.reload(fill_years)
    importlib.reload(fix_artist_title)

    changed = []
    errors = []
    for label, fn, key in (
        ("genre", lambda: fill_genres.fill_missing_genres(track_ids=[track_id]), "found"),
        ("year", lambda: fill_years.fix_release_years(track_ids=[track_id]), "updated"),
        ("artist/title", lambda: fix_artist_title.fix_artist_title(track_ids=[track_id]), "updated"),
    ):
        try:
            if fn().get(key):
                changed.append(label)
        except Exception as e:
            errors.append(f"{label}: {e}")

    db = get_db()
    row = db.execute(
        f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id WHERE t.id=?", (track_id,)
    ).fetchone()
    if not row:
        abort(404)
    return jsonify({"ok": True, "changed": changed, "errors": errors, "track": track_to_dict(row)})


TAG_AUDIT_CHECKS = {
    "genre": "(t.primary_genre IS NULL OR t.primary_genre = '')",
    "album": "(t.album IS NULL OR t.album = '')",
    "year": "t.year IS NULL",
}


@app.route("/api/tags/audit")
def tags_audit():
    """Fast, DB-only audit for tag gaps that are unambiguous (missing means
    NULL/empty in the index, no filename-fallback guesswork involved, unlike
    artist/title which scan_library always backfills from the path)."""
    db = get_db()
    result = {}
    for key, condition in TAG_AUDIT_CHECKS.items():
        rows = db.execute(
            f"SELECT t.id, t.artist, t.title, t.album, t.year, t.primary_genre as genre "
            f"FROM tracks t WHERE {condition} "
            f"ORDER BY t.artist COLLATE NOCASE, t.title COLLATE NOCASE"
        ).fetchall()
        tracks = [dict(r) for r in rows]
        result[key] = {"count": len(tracks), "tracks": tracks}
    return jsonify(result)


def _write_file_tag(fpath, field, value):
    """Writes a single artist/album/title/genre/year field to the audio
    file's own tags, format-specific like fill_genres.py's genre writer."""
    lower = fpath.lower()
    try:
        if lower.endswith(".flac"):
            from mutagen.flac import FLAC
            audio = FLAC(fpath)
            key = {"artist": "artist", "album": "album", "title": "title",
                   "genre": "genre", "year": "date"}[field]
            audio[key] = [str(value)]
            audio.save()
        elif lower.endswith(".mp3"):
            import mutagen
            from mutagen.id3 import ID3NoHeaderError
            from mutagen.easyid3 import EasyID3
            try:
                audio = EasyID3(fpath)
            except ID3NoHeaderError:
                audio = mutagen.File(fpath, easy=True)
                audio.add_tags()
            key = {"artist": "artist", "album": "album", "title": "title",
                   "genre": "genre", "year": "date"}[field]
            audio[key] = [str(value)]
            audio.save()
        elif lower.endswith(".m4a"):
            from mutagen.mp4 import MP4
            audio = MP4(fpath)
            key = {"artist": "\xa9ART", "album": "\xa9alb", "title": "\xa9nam",
                   "genre": "\xa9gen", "year": "\xa9day"}[field]
            audio[key] = [str(value)]
            audio.save()
        else:
            return False
        return True
    except Exception:
        return False


@app.route("/api/tags/<int:track_id>", methods=["POST"])
def update_tag(track_id):
    """Writes one tag field to a track's file and the local index. Only the
    fields the tag-checker checklist surfaces are allowed."""
    data = request.get_json(force=True, silent=True) or {}
    field = data.get("field")
    value = (data.get("value") or "").strip()
    if field not in ("genre", "album", "year", "artist", "title") or not value:
        abort(400)

    db = get_db()
    row = db.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)
    fpath = os.path.join(MUSIC_DIR, row["path"])
    if not os.path.isfile(fpath):
        abort(404)

    if not _write_file_tag(fpath, field, value):
        return jsonify({"ok": False, "error": "Could not write to the file"}), 500

    if field == "genre":
        db.execute("UPDATE tracks SET genre=?, primary_genre=? WHERE id=?", (value, value, track_id))
    elif field == "year":
        try:
            year_int = int(value)
        except ValueError:
            abort(400)
        db.execute("UPDATE tracks SET year=?, decade=? WHERE id=?", (year_int, (year_int // 10) * 10, track_id))
    elif field == "artist":
        db.execute("UPDATE tracks SET artist=?, has_artist_tag=1 WHERE id=?", (value, track_id))
    elif field == "title":
        db.execute("UPDATE tracks SET title=?, has_title_tag=1 WHERE id=?", (value, track_id))
    else:
        db.execute(f"UPDATE tracks SET {field}=? WHERE id=?", (value, track_id))
    db.commit()
    return jsonify({"ok": True, "track_id": track_id, "field": field, "value": value})


def _read_raw_tag_presence(fpath):
    """Reads a file's artist/title/cover-art presence straight from its own
    tags, with no filename/folder fallback -- unlike the fields cached in
    the tracks table, which scan_library always backfills from the path so
    a missing artist or title tag never actually shows up empty there. On
    read failure or an unrecognized format, reports "present" for all three
    rather than risk a false "missing" flag."""
    lower = fpath.lower()
    try:
        if lower.endswith(".flac"):
            from mutagen.flac import FLAC
            audio = FLAC(fpath)
            return bool(audio.get("artist")), bool(audio.get("title")), bool(audio.pictures)
        if lower.endswith(".mp3"):
            from mutagen.id3 import ID3
            tags = ID3(fpath)
            return bool(tags.getall("TPE1")), bool(tags.getall("TIT2")), bool(tags.getall("APIC"))
        if lower.endswith(".m4a"):
            from mutagen.mp4 import MP4
            audio = MP4(fpath)
            t = audio.tags or {}
            return bool(t.get("\xa9ART")), bool(t.get("\xa9nam")), bool(t.get("covr"))
    except Exception:
        pass
    return True, True, True


@app.route("/api/tags/deep-scan", methods=["POST"])
def tags_deep_scan():
    """Opens every file directly to check for a real artist tag, title tag,
    and embedded cover art -- slow (a full pass over the library), unlike
    the instant DB-only audit above. Results are cached on the tracks table
    so this only needs to re-run when the user explicitly asks for it."""
    db = get_db()
    rows = db.execute("SELECT id, path FROM tracks").fetchall()
    checked = 0
    for row in rows:
        fpath = os.path.join(MUSIC_DIR, row["path"])
        has_artist, has_title, has_art = (
            _read_raw_tag_presence(fpath) if os.path.isfile(fpath) else (True, True, True)
        )
        db.execute(
            "UPDATE tracks SET has_artist_tag=?, has_title_tag=?, has_art=? WHERE id=?",
            (int(has_artist), int(has_title), int(has_art), row["id"]),
        )
        checked += 1
        if checked % 500 == 0:
            db.commit()
    db.commit()

    result = {"checked": checked}
    for key, col in (("artist", "has_artist_tag"), ("title", "has_title_tag"), ("art", "has_art")):
        found = db.execute(
            f"SELECT id, artist, title, album, year, primary_genre as genre FROM tracks "
            f"WHERE {col} = 0 ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE"
        ).fetchall()
        tracks = [dict(r) for r in found]
        result[key] = {"count": len(tracks), "tracks": tracks}
    return jsonify(result)


@app.route("/api/config")
def get_config():
    return jsonify({"music_dir": MUSIC_DIR, "music_dir_exists": bool(MUSIC_DIR and os.path.isdir(MUSIC_DIR))})


@app.route("/api/theme")
def get_theme():
    return jsonify({"theme": jukebox_config.load_config().get("theme")})


@app.route("/api/theme", methods=["POST"])
def set_theme():
    """Saved server-side (not just localStorage) so the last theme used
    carries over in the pywebview desktop app too -- its WKWebView doesn't
    persist localStorage across separate app launches the way a real
    browser tab does, so localStorage alone silently resets there."""
    data = request.get_json(force=True, silent=True) or {}
    theme = data.get("theme")
    if not isinstance(theme, str) or not theme:
        abort(400)
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("theme", theme))
    return jsonify({"ok": True})


@app.route("/api/wood-finish")
def get_wood_finish():
    return jsonify({"woodFinish": jukebox_config.load_config().get("woodFinish")})


@app.route("/api/wood-finish", methods=["POST"])
def set_wood_finish():
    """The vinyl theme's turntable wood finish -- same persistence story as
    /api/theme above."""
    data = request.get_json(force=True, silent=True) or {}
    wood_finish = data.get("woodFinish")
    if not isinstance(wood_finish, str) or not wood_finish:
        abort(400)
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("woodFinish", wood_finish))
    return jsonify({"ok": True})


@app.route("/api/cassette-design")
def get_cassette_design():
    return jsonify({"cassetteDesign": jukebox_config.load_config().get("cassetteDesign")})


@app.route("/api/cassette-design", methods=["POST"])
def set_cassette_design():
    """The cassette theme's shell design (color band/label/shell material)
    -- same persistence story as /api/theme and /api/wood-finish above."""
    data = request.get_json(force=True, silent=True) or {}
    cassette_design = data.get("cassetteDesign")
    if not isinstance(cassette_design, str) or not cassette_design:
        abort(400)
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("cassetteDesign", cassette_design))
    return jsonify({"ok": True})


@app.route("/api/vu-color")
def get_vu_color():
    return jsonify({"vuColor": jukebox_config.load_config().get("vuColor")})


@app.route("/api/vu-color", methods=["POST"])
def set_vu_color():
    """The Hi-Fi theme's VU meter backlight (and, via --accent, every
    button/highlight in that theme) -- same persistence story as
    /api/theme and /api/wood-finish above."""
    data = request.get_json(force=True, silent=True) or {}
    vu_color = data.get("vuColor")
    if not isinstance(vu_color, str) or not vu_color:
        abort(400)
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("vuColor", vu_color))
    return jsonify({"ok": True})


def _pick_folder_dialog():
    """Native folder picker, per OS: AppleScript on macOS (no extra deps),
    Tk's file dialog everywhere else (bundled with the standard library, so
    it's available even in a PyInstaller-frozen build with no other GUI
    toolkit around). Returns the picked path, or None if cancelled."""
    if sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose folder with prompt "Select the music folder for Notorious B.P.M. to scan")'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None  # covers both "user cancelled" and any osascript error
        return result.stdout.strip().rstrip("/") or None

    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        picked = filedialog.askdirectory(title="Select the music folder for Notorious B.P.M. to scan")
    finally:
        root.destroy()
    return picked or None


@app.route("/api/choose-folder", methods=["POST"])
def choose_folder():
    global MUSIC_DIR
    picked = _pick_folder_dialog()
    if not picked or not os.path.isdir(picked):
        return jsonify({"ok": False, "cancelled": True})

    changed = picked != MUSIC_DIR
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("music_dir", picked))
    MUSIC_DIR = picked
    os.environ["JUKEBOX_MUSIC_DIR"] = picked

    if changed:
        # Switching to a different music folder wipes the whole library
        # index below (old paths are meaningless once MUSIC_DIR changes) --
        # snapshot it first, same as every other bulk-destructive action, so
        # ratings/playlists/play-counts for the previous folder aren't just
        # gone if this was picked by mistake. The wipe itself is fast and
        # stays synchronous; only the actual scan (which can take minutes on
        # a large library) runs in the background -- same _start_scan_bg()
        # used by /api/rescan, so the frontend polls /api/scan-progress
        # either way instead of blocking on this request.
        _snapshot_db()
        close_db(None)
        if os.path.isfile(DB_PATH):
            os.remove(DB_PATH)
        if os.path.isdir(ART_CACHE_DIR):
            shutil.rmtree(ART_CACHE_DIR)
        os.makedirs(ART_CACHE_DIR, exist_ok=True)
    else:
        # Re-picking the folder that's already configured used to be a
        # silent no-op here -- which looked exactly like "nothing happens"
        # to a user re-selecting a drive to make sure it gets scanned (e.g.
        # after a first attempt they weren't sure had finished). Treat this
        # the same as clicking Rescan instead: no destructive DB wipe (the
        # existing index for this folder is still meaningful), but always
        # actually run a scan so picking a folder always visibly does
        # something.
        _snapshot_db()

    started = _start_scan_bg()
    return jsonify({"ok": True, "changed": changed, "music_dir": picked, "started": started})


@app.route("/api/convert/status")
def convert_status():
    import importlib
    import convert_audio
    importlib.reload(convert_audio)
    return jsonify({
        "available": convert_audio.ffmpeg_available(),
        "output_root": convert_audio.OUTPUT_ROOT,
        "formats": convert_audio.FORMATS,
    })


def _convert_track_row(db, track_id, fmt):
    import importlib
    import convert_audio
    importlib.reload(convert_audio)
    row = db.execute("SELECT path, artist, title, ext FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        return {"track_id": track_id, "ok": False, "error": "track not found"}
    src_ext = row["ext"].lstrip(".").lower()
    src_fmt = {"flac": "flac", "m4a": "alac", "mp3": "mp3320"}.get(src_ext)
    if src_fmt == fmt:
        return {"track_id": track_id, "ok": False, "error": f"already {convert_audio.FORMATS[fmt]['label']}"}
    fpath = os.path.join(MUSIC_DIR, row["path"])
    try:
        out_path, already_existed = convert_audio.convert(fpath, row["artist"], row["title"], fmt)
        return {"track_id": track_id, "ok": True, "output_path": out_path, "already_existed": already_existed}
    except Exception as e:
        return {"track_id": track_id, "ok": False, "error": str(e)}


@app.route("/api/convert/<int:track_id>", methods=["POST"])
def convert_track(track_id):
    data = request.get_json(force=True, silent=True) or {}
    fmt = data.get("format")
    import importlib
    import convert_audio
    importlib.reload(convert_audio)
    if fmt not in convert_audio.FORMATS:
        abort(400)
    db = get_db()
    result = _convert_track_row(db, track_id, fmt)
    return jsonify(result)


@app.route("/api/convert-tracks", methods=["POST"])
def convert_tracks():
    data = request.get_json(force=True, silent=True) or {}
    fmt = data.get("format")
    track_ids = data.get("track_ids") or []
    import importlib
    import convert_audio
    importlib.reload(convert_audio)
    if fmt not in convert_audio.FORMATS or not track_ids:
        abort(400)
    db = get_db()
    results = [_convert_track_row(db, tid, fmt) for tid in track_ids]
    ok = sum(1 for r in results if r["ok"])
    return jsonify({"total": len(results), "converted": ok, "failed": len(results) - ok, "results": results})


@app.route("/api/playlists/<int:playlist_id>/convert", methods=["POST"])
def convert_playlist(playlist_id):
    data = request.get_json(force=True, silent=True) or {}
    fmt = data.get("format")
    import importlib
    import convert_audio
    importlib.reload(convert_audio)
    if fmt not in convert_audio.FORMATS:
        abort(400)
    db = get_db()
    track_ids = [r["track_id"] for r in db.execute(
        "SELECT track_id FROM playlist_tracks WHERE playlist_id=? ORDER BY position", (playlist_id,)
    ).fetchall()]
    results = [_convert_track_row(db, tid, fmt) for tid in track_ids]
    ok = sum(1 for r in results if r["ok"])
    return jsonify({"total": len(results), "converted": ok, "failed": len(results) - ok, "results": results})


@app.route("/api/facets")
def facets():
    db = get_db()
    genres = [r[0] for r in db.execute(
        "SELECT DISTINCT primary_genre FROM tracks WHERE primary_genre IS NOT NULL "
        "AND primary_genre != '' ORDER BY primary_genre COLLATE NOCASE"
    ).fetchall()]
    decades = [r[0] for r in db.execute(
        "SELECT DISTINCT decade FROM tracks WHERE decade IS NOT NULL ORDER BY decade"
    ).fetchall()]
    artists = [r[0] for r in db.execute(
        "SELECT DISTINCT artist FROM tracks WHERE artist IS NOT NULL ORDER BY artist COLLATE NOCASE"
    ).fetchall()]
    languages = [r[0] for r in db.execute(
        "SELECT DISTINCT language FROM tracks WHERE language IS NOT NULL AND language != '' "
        "ORDER BY CASE language WHEN 'Croatian' THEN 1 WHEN 'English' THEN 2 "
        "WHEN 'French' THEN 3 WHEN 'Italian' THEN 4 WHEN 'Spanish' THEN 5 "
        "WHEN 'Other' THEN 6 ELSE 7 END"
    ).fetchall()]
    total = db.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    return jsonify({"genres": genres, "decades": decades, "artists": artists,
                     "languages": languages, "total": total})


# ---------------------------------------------------------------- duplicates --

_DUP_BRACKET_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_DUP_EDITION_WORDS = (
    r"live|remaster(?:ed)?(?:\s*\d{2,4})?|radio edit|single version|album version|"
    r"remix|acoustic|demo|mono|stereo|extended(?:\s+mix|\s+version)?|instrumental|"
    r"explicit|clean|deluxe(?:\s+edition)?|bonus track|edit|version|"
    r"\d{4}(?:\s+remaster(?:ed)?)?"
)
_DUP_DASH_SUFFIX_RE = re.compile(rf"\s*-\s*(?:{_DUP_EDITION_WORDS})\s*$", re.IGNORECASE)


def _normalize_dup_title(title):
    if not title:
        return ""
    t = _DUP_BRACKET_RE.sub(" ", title)
    prev = None
    while prev != t:
        prev = t
        t = _DUP_DASH_SUFFIX_RE.sub("", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _normalize_dup_artist(artist):
    return (artist or "").strip().lower()


def _find_duplicate_groups(db):
    """Groups tracks that look like the same song under different editions
    (Live, Remastered, Radio Edit, ...), sorted by artist. Shared by the
    review listing and the auto-clean bulk action below."""
    rows = db.execute(
        f"SELECT {TRACK_FIELDS}, t.path FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id "
        f"ORDER BY t.artist COLLATE NOCASE, t.title COLLATE NOCASE"
    ).fetchall()

    groups = {}
    for row in rows:
        d = track_to_dict(row)
        title_norm = _normalize_dup_title(d["title"])
        if not title_norm:
            continue
        key = (_normalize_dup_artist(d["artist"]), title_norm)
        groups.setdefault(key, []).append(d)

    result = []
    for (_artist_norm, title_norm), tracks in groups.items():
        if len(tracks) < 2:
            continue
        result.append({
            "artist": tracks[0]["artist"],
            "title_base": title_norm,
            "tracks": tracks,
        })
    result.sort(key=lambda g: (g["artist"] or "").lower())
    return result


@app.route("/api/duplicates")
def duplicates():
    """Paginated listing of duplicate groups for manual review."""
    db = get_db()
    result = _find_duplicate_groups(db)
    total_groups = len(result)
    total_tracks = sum(len(g["tracks"]) for g in result)

    limit = min(int(request.args.get("limit", 30)), 100)
    offset = int(request.args.get("offset", 0))
    page = result[offset:offset + limit]

    return jsonify({
        "groups": page,
        "total_groups": total_groups,
        "total_tracks": total_tracks,
    })


_DUP_NON_ALBUM_RE = re.compile(r"\blive\b|remaster|\bmix(?:es)?\b|\bremix(?:es)?\b", re.IGNORECASE)


def _is_non_album_version(track):
    text = f"{track['title'] or ''} {track['album'] or ''}"
    return bool(_DUP_NON_ALBUM_RE.search(text))


_DUP_SUFFIX_RE = re.compile(r"^(.*)\((\d+)\)(\.[A-Za-z0-9]+)$")


def _find_repeat_download_dupes(db):
    """Catches literal re-downloaded copies: same folder, filename differs
    only by a "(1)", "(2)", ... suffix -- what a downloader appends to avoid
    overwriting a file that's already there. This is a much stronger signal
    than title matching (same bytes-ish file, not just the same song), and
    catches duplicates _find_duplicate_groups's title grouping misses
    entirely -- e.g. two copies of the exact same album track, which has no
    Live/Remastered/Mix edition for the existing rule to key off of.
    Keeps the plain (unsuffixed) file when one exists, else the
    lowest-numbered copy; every other copy in the cluster is returned for
    removal, alongside a summary of what was kept vs removed per cluster."""
    rows = db.execute(f"SELECT {TRACK_FIELDS}, t.path FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id").fetchall()
    by_dir = {}
    for row in rows:
        t = track_to_dict(row)
        path = t.get("path") or ""
        directory, base = os.path.split(path)
        by_dir.setdefault(directory, {})[base] = t

    to_delete = []
    clean_groups = []
    for directory, files in by_dir.items():
        clusters = {}
        for base, t in files.items():
            m = _DUP_SUFFIX_RE.match(base)
            if m and (m.group(1) + m.group(3)) in files:
                # Only cluster a "(N)" file with its plain counterpart when
                # that counterpart actually exists -- a lone "Track(1).m4a"
                # with no "Track.m4a" alongside it is just this file's real
                # name, not a duplicate marker.
                canonical = m.group(1) + m.group(3)
                num = int(m.group(2))
            else:
                canonical = base
                num = 0
            clusters.setdefault(canonical, []).append((num, t))
        for canonical, entries in clusters.items():
            if len(entries) < 2:
                continue
            entries.sort(key=lambda e: e[0])
            keep = entries[0][1]
            dupes = [t for _, t in entries[1:]]
            to_delete.extend(dupes)
            clean_groups.append({
                "artist": keep["artist"],
                "kept_title": keep["title"],
                "removed_titles": [t["title"] for t in dupes],
            })
    return to_delete, clean_groups


def _plan_auto_clean(db):
    """For each duplicate group that has both a plain (album) version and at
    least one Live/Remastered/Mix version, delete only the Live/Remastered/
    Mix ones -- every plain version is left untouched, so this can never
    remove someone's only copy of a studio track. Groups with no plain
    version (nothing to prefer) or no non-album version (nothing to remove)
    are skipped, and reported back with a reason so they can be reviewed.
    Separately, repeat-downloaded copies (see _find_repeat_download_dupes)
    are folded into the same removal list, since those are just as safe to
    clean up automatically and are often the bulk of a library's actual
    duplicates -- titles/albums that are identical rather than an edition
    variant, which the title-based grouping above never flags at all."""
    groups = _find_duplicate_groups(db)
    to_delete = []
    groups_cleaned = 0
    skipped = []  # (group, reason) -- filtered against repeat-download results below
    for group in groups:
        plain = [t for t in group["tracks"] if not _is_non_album_version(t)]
        non_album = [t for t in group["tracks"] if _is_non_album_version(t)]
        if not plain or not non_album:
            skipped.append((group, "all_non_album" if not plain else "no_edition_to_remove"))
            continue
        groups_cleaned += 1
        to_delete.extend(non_album)

    seen_ids = {t["id"] for t in to_delete}
    repeat_dupes, repeat_groups = _find_repeat_download_dupes(db)
    for t in repeat_dupes:
        if t["id"] not in seen_ids:
            to_delete.append(t)
            seen_ids.add(t["id"])

    # A title-matched group only stays "skipped" if it still has 2+ tracks
    # that neither rule touched -- one the repeat-download pass is also
    # cleaning up (e.g. two identical "Greatest Hits" copies, no edition
    # variant in sight) is no longer sitting there untouched, so don't
    # report it as left alone.
    skipped_groups = []
    for group, reason in skipped:
        remaining = [t for t in group["tracks"] if t["id"] not in seen_ids]
        if len(remaining) >= 2:
            skipped_groups.append({
                "artist": group["artist"],
                "titles": [t["title"] for t in group["tracks"]],
                "reason": reason,
                # Full track objects (id, album, year, duration, ...) for
                # the manual review screen -- the summary/preview response
                # only ever reads "titles"/"reason" above, so this doesn't
                # change what that already-shipped response looks like.
                "tracks": remaining,
            })

    return to_delete, groups_cleaned, skipped_groups, len(repeat_groups)


_dup_clean_state = {"running": False, "done": 0, "total": 0, "deleted": 0, "errors": None, "error": None}
_dup_clean_lock = threading.Lock()


def _run_dup_clean_bg(rows):
    def progress_cb(done, total):
        _dup_clean_state["done"] = done
        _dup_clean_state["total"] = total

    try:
        # A fresh connection, not get_db()'s Flask-request-scoped one --
        # this runs on a background thread with no request context.
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            deleted, errors = _delete_track_rows(conn, rows, progress_cb=progress_cb)
            _dup_clean_state["deleted"] = deleted
            _dup_clean_state["errors"] = errors
        finally:
            conn.close()
    except Exception as e:
        _dup_clean_state["error"] = str(e)
    finally:
        _dup_clean_state["running"] = False


@app.route("/api/duplicates/auto-clean", methods=["POST"])
def duplicates_auto_clean():
    """One-click cleanup: keep one album version of each song, remove every
    Live/Remastered/Mix version, plus any literal repeat-downloaded copies
    (same folder, filename differing only by a "(1)"/"(2)"/... suffix).
    `dry_run` (default true) previews counts without deleting anything.

    The real (dry_run=false) pass runs in the background with progress
    polling, same as fill-genres/fill-years: when the music folder is on a
    different volume than the app's own data, each "delete" is a real
    cross-filesystem copy -- for hundreds of lossless files that can take
    minutes, and a plain blocking request with no progress shown looks
    indistinguishable from broken."""
    data = request.get_json(force=True, silent=True) or {}
    dry_run = data.get("dry_run", True)

    db = get_db()
    to_delete, groups_cleaned, skipped_groups, repeat_groups_cleaned = _plan_auto_clean(db)
    groups_skipped = len(skipped_groups)

    if dry_run:
        return jsonify({
            "tracks_to_delete": len(to_delete),
            "groups_cleaned": groups_cleaned,
            "groups_skipped": groups_skipped,
            "repeat_groups_cleaned": repeat_groups_cleaned,
            "tracks": [{"artist": t["artist"], "title": t["title"]} for t in to_delete],
            "skipped_groups": skipped_groups,
        })

    with _dup_clean_lock:
        if _dup_clean_state["running"]:
            return jsonify({"started": False, "error": "Already running"})
        _dup_clean_state.update(running=True, done=0, total=len(to_delete), deleted=0, errors=None, error=None)
        close_db(None)
        threading.Thread(target=_run_dup_clean_bg, args=(to_delete,), daemon=True).start()
    return jsonify({"started": True, "total": len(to_delete)})


@app.route("/api/duplicates/auto-clean/progress")
def duplicates_auto_clean_progress():
    return jsonify(_dup_clean_state)


@app.route("/api/duplicates/review")
def duplicates_review():
    """Paginated listing of duplicate groups that neither auto-clean rule
    could confidently resolve on its own (same song, multiple copies, but
    no clearly-lesser edition and no literal repeat-downloaded file) -- for
    a human to look through and pick which copy to keep."""
    db = get_db()
    _to_delete, _groups_cleaned, skipped_groups, _repeat_groups_cleaned = _plan_auto_clean(db)

    limit = min(int(request.args.get("limit", 20)), 50)
    offset = int(request.args.get("offset", 0))
    page = skipped_groups[offset:offset + limit]

    return jsonify({
        "groups": [
            {"artist": g["artist"], "reason": g["reason"], "tracks": g["tracks"]}
            for g in page
        ],
        "total_groups": len(skipped_groups),
    })


def _build_track_filter(args=None):
    """Reads filter params (q, artist, genre, decade, language, rated_only,
    rating) and returns (where_sql, params). `args` defaults to the current
    request's query string but also accepts a plain dict, so smart playlists
    can reuse the exact same filter logic against their saved rules. genre/
    decade/language accept comma-separated values for multi-select."""
    args = request.args if args is None else args

    def _get(key, default=""):
        v = args.get(key, default)
        return v.strip() if isinstance(v, str) else v

    q = _get("q")
    artist = _get("artist")
    genre = _get("genre")
    decade = _get("decade")
    language = _get("language")
    rated_only = _get("rated_only") in ("1", True)
    rating = _get("rating")
    rating_min = _get("rating_min")

    where = []
    params = []
    if q:
        db = get_db()
        has_fts = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='tracks_fts'"
        ).fetchone()[0] > 0
        fts_query = " ".join(f'"{tok.replace(chr(34), chr(34) * 2)}"*' for tok in q.split())
        if has_fts and fts_query:
            where.append("t.id IN (SELECT rowid FROM tracks_fts WHERE tracks_fts MATCH ?)")
            params.append(fts_query)
        else:
            where.append("(t.artist LIKE ? OR t.title LIKE ? OR t.album LIKE ?)")
            like = f"%{q}%"
            params += [like, like, like]
    if artist:
        where.append("t.artist = ?")
        params.append(artist)
    if genre:
        values = [v for v in str(genre).split(",") if v]
        where.append(f"t.primary_genre IN ({','.join('?' * len(values))})")
        params += values
    if decade:
        values = [int(v) for v in str(decade).split(",") if v]
        where.append(f"t.decade IN ({','.join('?' * len(values))})")
        params += values
    if language:
        values = [v for v in str(language).split(",") if v]
        where.append(f"t.language IN ({','.join('?' * len(values))})")
        params += values
    if rated_only:
        where.append("r.rating IS NOT NULL")
    if rating:
        where.append("r.rating = ?")
        params.append(int(rating))
    if rating_min:
        where.append("r.rating >= ?")
        params.append(int(rating_min))

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


@app.route("/api/track-ids")
def track_ids():
    db = get_db()
    where_sql, params = _build_track_filter()
    rows = db.execute(
        f"SELECT t.id FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id {where_sql}",
        params,
    ).fetchall()
    return jsonify({"ids": [r[0] for r in rows], "total": len(rows)})


@app.route("/api/tracks")
def tracks():
    db = get_db()
    sort = request.args.get("sort", "artist")
    limit = min(int(request.args.get("limit", 100)), 500)
    offset = int(request.args.get("offset", 0))

    where_sql, params = _build_track_filter()
    sort_map = {
        "artist": "t.artist COLLATE NOCASE, t.album COLLATE NOCASE, t.title COLLATE NOCASE",
        "genre": "t.primary_genre COLLATE NOCASE, t.artist COLLATE NOCASE, t.title COLLATE NOCASE",
        "title": "t.title COLLATE NOCASE",
        "year": "t.year, t.artist COLLATE NOCASE",
        "rating": "r.rating DESC NULLS LAST, t.artist COLLATE NOCASE",
    }
    order_sql = sort_map.get(sort, sort_map["artist"])

    count_row = db.execute(
        f"SELECT COUNT(*) FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id {where_sql}",
        params,
    ).fetchone()
    total = count_row[0]

    rows = db.execute(
        f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id "
        f"{where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()

    return jsonify({"total": total, "tracks": [track_to_dict(r) for r in rows]})


@app.route("/api/stream/<int:track_id>")
def stream(track_id):
    db = get_db()
    row = db.execute("SELECT path, ext FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)
    fpath = os.path.join(MUSIC_DIR, row["path"])
    if not os.path.isfile(fpath):
        abort(404)
    mimetypes = {".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
                 ".wav": "audio/wav", ".ogg": "audio/ogg"}
    return send_file(fpath, mimetype=mimetypes.get(row["ext"], "application/octet-stream"),
                      conditional=True)


def _extract_art(fpath):
    """Pull embedded cover art bytes + mime type out of an audio file's tags."""
    lower = fpath.lower()
    try:
        if lower.endswith(".flac"):
            from mutagen.flac import FLAC
            audio = FLAC(fpath)
            if audio.pictures:
                pic = audio.pictures[0]
                return pic.data, pic.mime or "image/jpeg"
        elif lower.endswith(".mp3"):
            from mutagen.id3 import ID3
            tags = ID3(fpath)
            apics = tags.getall("APIC")
            if apics:
                return apics[0].data, apics[0].mime or "image/jpeg"
        elif lower.endswith(".m4a"):
            from mutagen.mp4 import MP4
            audio = MP4(fpath)
            covr = audio.tags.get("covr") if audio.tags else None
            if covr:
                c = covr[0]
                mime = "image/png" if c.imageformat == c.FORMAT_PNG else "image/jpeg"
                return bytes(c), mime
    except Exception:
        pass
    return None, None


def get_art(track_id, fpath):
    """Cache-backed lookup of a track's embedded cover art on disk."""
    for ext, mime in ((".jpg", "image/jpeg"), (".png", "image/png")):
        cached = os.path.join(ART_CACHE_DIR, f"{track_id}{ext}")
        if os.path.isfile(cached):
            with open(cached, "rb") as f:
                return f.read(), mime
    if os.path.isfile(os.path.join(ART_CACHE_DIR, f"{track_id}.none")):
        return None, None

    data, mime = _extract_art(fpath)
    if data:
        ext = ".png" if mime == "image/png" else ".jpg"
        with open(os.path.join(ART_CACHE_DIR, f"{track_id}{ext}"), "wb") as f:
            f.write(data)
    else:
        open(os.path.join(ART_CACHE_DIR, f"{track_id}.none"), "wb").close()
    return data, mime


@app.route("/api/art/<int:track_id>")
def art(track_id):
    db = get_db()
    row = db.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)
    fpath = os.path.join(MUSIC_DIR, row["path"])
    data, mime = get_art(track_id, fpath)
    if not data:
        abort(404)
    resp = make_response(data)
    resp.headers["Content-Type"] = mime
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.route("/api/art/<int:track_id>/fetch", methods=["POST"])
def fetch_art(track_id):
    """Best-effort cover art from Deezer for a track with no embedded art --
    caches straight into ART_CACHE_DIR (the same place get_art already
    checks first) without ever touching the source audio file, so a bad
    match or a failed write can't corrupt anything."""
    import fill_genres
    db = get_db()
    row = db.execute("SELECT artist, title FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)
    results = fill_genres._http_json(fill_genres.DEEZER_SEARCH, {
        "q": f'artist:"{(row["artist"] or "").strip()}" track:"{(row["title"] or "").strip()}"',
        "limit": 1,
    })
    candidates = (results or {}).get("data") or []
    cover_url = candidates[0].get("album", {}).get("cover_big") if candidates else None
    if not cover_url:
        return jsonify({"ok": False, "error": "No match found"}), 404

    import urllib.request
    try:
        with urllib.request.urlopen(cover_url, timeout=10) as resp:
            image_bytes = resp.read()
    except Exception:
        return jsonify({"ok": False, "error": "Couldn't download the image"}), 502

    dest = os.path.join(ART_CACHE_DIR, f"{track_id}.jpg")
    with open(dest, "wb") as f:
        f.write(image_bytes)
    none_marker = os.path.join(ART_CACHE_DIR, f"{track_id}.none")
    if os.path.isfile(none_marker):
        os.remove(none_marker)
    db.execute("UPDATE tracks SET has_art=1 WHERE id=?", (track_id,))
    db.commit()
    return jsonify({"ok": True, "track_id": track_id})


# ----------------------------------------------------------------- ratings --

@app.route("/api/rate/<int:track_id>", methods=["POST"])
def rate(track_id):
    data = request.get_json(force=True, silent=True) or {}
    rating = data.get("rating")
    db = get_db()
    # A non-numeric rating used to reach int(rating) unguarded and raise
    # ValueError before this could reject it with a clean 400 (JB-002).
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        abort(400)
    if not (0 <= rating <= 5):
        abort(400)
    # An unknown track_id used to reach the INSERT below and raise a raw
    # sqlite3.IntegrityError (foreign key violation) instead of a clean 404
    # (JB-002) -- e.g. a stale id from a client that hasn't refreshed since
    # the last rescan removed the track.
    if not db.execute("SELECT 1 FROM tracks WHERE id=?", (track_id,)).fetchone():
        abort(404)
    if rating == 0:
        db.execute("DELETE FROM ratings WHERE track_id=?", (track_id,))
    else:
        db.execute(
            "INSERT INTO ratings (track_id, rating, rated_at) VALUES (?,?,?) "
            "ON CONFLICT(track_id) DO UPDATE SET rating=excluded.rating, rated_at=excluded.rated_at",
            (track_id, rating, datetime.datetime.utcnow().isoformat()),
        )
    db.commit()
    return jsonify({"ok": True, "track_id": track_id, "rating": rating})


def _lookup_lyrics(artist, title):
    """Best-effort lyrics from lrclib.net's free, keyless public API. Shared
    by the library lookup (cached to disk per track_id) and the internet
    radio lookup (by artist/title text, nothing to key a disk cache on)."""
    # /api/search (fuzzy, multiple candidates) rather than /api/get (exact
    # match on album+duration too) -- tracks stored under a compilation or
    # remaster's album name almost never match lrclib's own album field
    # exactly, so /api/get returns nothing even when the song is indexed.
    params = {"artist_name": artist or "", "track_name": title or ""}
    url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
    result = {"found": False}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": RADIO_UA})
        with urllib.request.urlopen(req, timeout=8) as resp:
            candidates = json.loads(resp.read().decode("utf-8"))
        match = next((c for c in candidates if c.get("plainLyrics") or c.get("instrumental")), None)
        if match:
            text = match.get("plainLyrics") or match.get("syncedLyrics")
            result = {"found": bool(text), "lyrics": text, "instrumental": bool(match.get("instrumental"))}
    except Exception:
        pass
    return result


@app.route("/api/lyrics/<int:track_id>")
def lyrics(track_id):
    db = get_db()
    row = db.execute("SELECT artist, title, album, duration FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)

    cache_path = os.path.join(ART_CACHE_DIR, f"{track_id}.lyrics.json")
    if os.path.isfile(cache_path):
        with open(cache_path) as f:
            return jsonify(json.load(f))

    result = _lookup_lyrics(row["artist"], row["title"])
    with open(cache_path, "w") as f:
        json.dump(result, f)
    return jsonify(result)


@app.route("/api/lyrics-by-name")
def lyrics_by_name():
    """Same lrclib lookup as /api/lyrics/<id>, but for internet radio --
    there's no track_id (or file) to look anything up from, just whatever
    artist/title the station's ICY metadata or the Identify button
    reported. Not disk-cached like the library version since there's no
    stable id to key a cache file on, and a radio "track" here is a
    transient, one-off thing anyway."""
    artist = request.args.get("artist", "")
    title = request.args.get("title", "")
    if not title:
        abort(400)
    return jsonify(_lookup_lyrics(artist, title))


@app.route("/api/plays/<int:track_id>", methods=["POST"])
def log_play(track_id):
    db = get_db()
    db.execute(
        "UPDATE tracks SET play_count = play_count + 1, last_played_at = ? WHERE id = ?",
        (datetime.datetime.utcnow().isoformat(), track_id),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/stats")
def listening_stats():
    db = get_db()
    top_tracks = db.execute(
        f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id "
        "WHERE t.play_count > 0 ORDER BY t.play_count DESC LIMIT 25"
    ).fetchall()
    recently_played = db.execute(
        f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id "
        "WHERE t.last_played_at IS NOT NULL ORDER BY t.last_played_at DESC LIMIT 25"
    ).fetchall()
    top_artists = db.execute(
        "SELECT artist, SUM(play_count) as plays FROM tracks WHERE play_count > 0 "
        "GROUP BY artist ORDER BY plays DESC LIMIT 10"
    ).fetchall()
    total_plays = db.execute("SELECT COALESCE(SUM(play_count), 0) FROM tracks").fetchone()[0]
    return jsonify({
        "total_plays": total_plays,
        "top_tracks": [track_to_dict(r) for r in top_tracks],
        "recently_played": [track_to_dict(r) for r in recently_played],
        "top_artists": [dict(r) for r in top_artists],
    })


def _delete_track_rows(db, rows, progress_cb=None):
    """Moves each track's audio file to the trash folder (never a hard
    delete), drops the cached art, records enough to restore it later, and
    removes the DB row. Snapshots the whole DB first so even a mistaken bulk
    action is fully recoverable. Shared by every deletion endpoint.

    When the music folder lives on a different volume than the app's own
    data (e.g. an external drive), each move is a real cross-filesystem
    copy+delete, not a fast rename -- for a few hundred lossless files that
    can take minutes with nothing to show for it, so this commits every 25
    files (both so a caller polling `progress_cb` sees real durable
    progress, and so an interrupted run doesn't lose track of files already
    physically moved to trash) instead of holding one big transaction open
    until the very end."""
    _snapshot_db()
    deleted = 0
    errors = []
    total = len(rows)
    for i, row in enumerate(rows):
        fpath = os.path.join(MUSIC_DIR, row["path"])
        trash_name = f"{row['id']}_{os.path.basename(row['path'])}"
        trash_dest = os.path.join(TRASH_DIR, trash_name)
        try:
            if not os.path.isfile(fpath):
                raise OSError(f"Source file not found: {fpath}")
            os.makedirs(TRASH_DIR, exist_ok=True)
            shutil.move(fpath, trash_dest)
        except OSError as e:
            # A missing source file used to fall through silently here --
            # the move was just skipped, but the code still went on to
            # record a trash entry and drop the library row as if it had
            # worked, leaving an unrestorable trash record pointing at a
            # file that was never actually moved (JB-001).
            errors.append({"track_id": row["id"], "path": row["path"], "error": str(e)})
            if progress_cb:
                progress_cb(i + 1, total)
            continue
        db.execute(
            "INSERT INTO trash (original_path, trash_path, artist, title, album, trashed_at) VALUES (?,?,?,?,?,?)",
            (row["path"], trash_dest, row["artist"] if "artist" in row.keys() else None,
             row["title"] if "title" in row.keys() else None,
             row["album"] if "album" in row.keys() else None,
             datetime.datetime.utcnow().isoformat()),
        )
        for ext in (".jpg", ".png", ".none"):
            cached = os.path.join(ART_CACHE_DIR, f"{row['id']}{ext}")
            if os.path.isfile(cached):
                os.remove(cached)
        db.execute("DELETE FROM tracks WHERE id=?", (row["id"],))
        deleted += 1
        if deleted % 25 == 0:
            db.commit()
        if progress_cb:
            progress_cb(i + 1, total)
    db.commit()
    return deleted, errors


@app.route("/api/delete-rated", methods=["POST"])
def delete_rated():
    """Move every file rated exactly `rating` to trash -- re-derives the
    matching tracks server-side from the DB rather than trusting a client-sent
    id list, so it can only ever act on tracks that actually carry that
    rating right now."""
    data = request.get_json(force=True, silent=True) or {}
    rating = data.get("rating")
    if rating is None or not (1 <= int(rating) <= 5):
        abort(400)
    rating = int(rating)

    db = get_db()
    rows = db.execute(
        "SELECT t.id, t.path, t.artist, t.title, t.album FROM tracks t "
        "JOIN ratings r ON r.track_id = t.id WHERE r.rating = ?",
        (rating,),
    ).fetchall()
    deleted, errors = _delete_track_rows(db, rows)
    return jsonify({"ok": True, "deleted": deleted, "errors": errors})


@app.route("/api/delete-tracks", methods=["POST"])
def delete_tracks_route():
    """Move an explicit set of tracks to trash by id -- used by the
    duplicate finder once the user has picked which version(s) to remove."""
    data = request.get_json(force=True, silent=True) or {}
    track_ids = data.get("track_ids") or []
    if not track_ids or not all(isinstance(t, int) for t in track_ids):
        abort(400)

    db = get_db()
    placeholders = ",".join("?" * len(track_ids))
    rows = db.execute(
        f"SELECT id, path, artist, title, album FROM tracks WHERE id IN ({placeholders})", track_ids
    ).fetchall()
    deleted, errors = _delete_track_rows(db, rows)
    return jsonify({"ok": True, "deleted": deleted, "errors": errors})


def _reindex_single_file(db, fpath, rel_path):
    """Re-inserts one restored file into `tracks`, reusing scan_library's own
    tag-parsing helpers so a restored track looks exactly like one found by
    a normal library scan."""
    import mutagen
    import scan_library

    ext = os.path.splitext(fpath)[1].lower()
    audio = mutagen.File(fpath, easy=True)
    tags = dict(audio.tags) if audio and audio.tags else {}
    artist = scan_library.first_or_none(tags, "artist") or os.path.basename(os.path.dirname(fpath))
    album = scan_library.first_or_none(tags, "album")
    title = scan_library.first_or_none(tags, "title") or os.path.splitext(os.path.basename(fpath))[0]
    genre_parts = []
    for g in tags.get("genre") or []:
        genre_parts.extend(str(g).split("\x00"))
    genre_parts = [g.strip() for g in genre_parts if g.strip()]
    seen = set()
    genre_parts = [g for g in genre_parts if not (g in seen or seen.add(g))]
    genre = "; ".join(genre_parts) if genre_parts else None
    primary_genre = genre_parts[0] if genre_parts else None
    year = scan_library.parse_year(tags)
    decade = (year // 10) * 10 if year else None
    bpm = scan_library.parse_bpm(tags)
    duration = getattr(audio.info, "length", None) if audio and audio.info else None
    language = scan_library.detect_language(title, artist, album)

    cur = db.execute(
        """INSERT INTO tracks (path, artist, album, title, genre, primary_genre,
           year, decade, bpm, duration, ext, language) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rel_path, artist, album, title, genre, primary_genre, year, decade, bpm, duration, ext, language),
    )
    return cur.lastrowid


# -------------------------------------------------------------------- trash --

@app.route("/api/trash")
def list_trash():
    db = get_db()
    rows = db.execute("SELECT * FROM trash ORDER BY trashed_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/trash/<int:trash_id>/restore", methods=["POST"])
def restore_trash(trash_id):
    db = get_db()
    row = db.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
    if not row:
        abort(404)
    dest = os.path.join(MUSIC_DIR, row["original_path"])
    if not os.path.isfile(row["trash_path"]):
        abort(404)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.isfile(dest):
        return jsonify({"ok": False, "error": "A file already exists at the original location"}), 409
    shutil.move(row["trash_path"], dest)

    track_id = _reindex_single_file(db, dest, row["original_path"])
    db.execute("DELETE FROM trash WHERE id=?", (trash_id,))
    db.commit()
    return jsonify({"ok": True, "track_id": track_id})


@app.route("/api/trash/<int:trash_id>", methods=["DELETE"])
def purge_trash_item(trash_id):
    db = get_db()
    row = db.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
    if not row:
        abort(404)
    if os.path.isfile(row["trash_path"]):
        os.remove(row["trash_path"])
    db.execute("DELETE FROM trash WHERE id=?", (trash_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/trash/empty", methods=["POST"])
def empty_trash():
    db = get_db()
    rows = db.execute("SELECT * FROM trash").fetchall()
    purged = 0
    for row in rows:
        if os.path.isfile(row["trash_path"]):
            os.remove(row["trash_path"])
        purged += 1
    db.execute("DELETE FROM trash")
    db.commit()
    return jsonify({"ok": True, "purged": purged})


# ------------------------------------------------------------- similarity --

def score_candidates(db, seed, exclude_ids, limit=25):
    """Return up to `limit` candidate rows scored by similarity to `seed` row."""
    params = []
    where = ["t.id != ?"]
    params.append(seed["id"])
    if exclude_ids:
        placeholders = ",".join("?" * len(exclude_ids))
        where.append(f"t.id NOT IN ({placeholders})")
        params += list(exclude_ids)

    rows = db.execute(
        f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id "
        f"WHERE {' AND '.join(where)}",
        params,
    ).fetchall()

    seed_bucket = tempo_bucket(seed["bpm"])
    scored = []
    for row in rows:
        d = track_to_dict(row)
        score = 0.0
        if seed["primary_genre"] and d["primary_genre"] == seed["primary_genre"]:
            score += 3
        if seed["decade"] is not None and d["decade"] == seed["decade"]:
            score += 2
        if seed_bucket and d["tempo_bucket"] == seed_bucket:
            score += 1
        if seed["language"] and d["language"] == seed["language"]:
            score += 1.5
        if seed["artist"] and d["artist"] == seed["artist"]:
            score += 0.5
        if d.get("rating"):
            score += (d["rating"] - 3) * 0.5
        if score > 0:
            scored.append((score, d))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


@app.route("/api/next")
def next_track():
    db = get_db()
    current_id = request.args.get("current", type=int)
    exclude = request.args.get("exclude", "")
    exclude_ids = [int(x) for x in exclude.split(",") if x.strip().isdigit()]
    if current_id:
        exclude_ids.append(current_id)

    seed = None
    if current_id:
        seed = db.execute(
            f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id WHERE t.id=?",
            (current_id,),
        ).fetchone()

    if seed:
        candidates = score_candidates(db, seed, exclude_ids)
        if candidates:
            weights = [max(c[0], 0.1) for c in candidates]
            chosen = random.choices(candidates, weights=weights, k=1)[0][1]
            return jsonify({"track": chosen, "reason": "similar"})

    # fallback: random track, weighted toward higher-rated ones
    placeholders = ",".join("?" * len(exclude_ids)) if exclude_ids else None
    where = f"WHERE t.id NOT IN ({placeholders})" if placeholders else ""
    rows = db.execute(
        f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id {where} "
        f"ORDER BY RANDOM() LIMIT 25",
        exclude_ids,
    ).fetchall()
    if not rows:
        return jsonify({"track": None, "reason": "empty"})
    weights = [1 + (r["rating"] or 3) for r in rows]
    chosen = random.choices(rows, weights=weights, k=1)[0]
    return jsonify({"track": track_to_dict(chosen), "reason": "random"})


@app.route("/api/radio")
def radio():
    """Build an on-the-go playlist -- a random shuffle of one genre (?genre=),
    or, failing that, tracks similar to a seed track (?seed=)."""
    db = get_db()
    genre = (request.args.get("genre") or "").strip()
    count = min(int(request.args.get("count", 30)), 100)
    if genre:
        rows = db.execute(
            f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id "
            f"WHERE t.primary_genre = ? ORDER BY RANDOM() LIMIT ?",
            (genre, count),
        ).fetchall()
        return jsonify({"tracks": [track_to_dict(r) for r in rows]})

    seed_id = request.args.get("seed", type=int)
    count = min(count, 50)
    if not seed_id:
        abort(400)
    seed = db.execute(
        f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id WHERE t.id=?",
        (seed_id,),
    ).fetchone()
    if not seed:
        abort(404)

    result = [track_to_dict(seed)]
    exclude_ids = [seed_id]
    current = seed
    for _ in range(count - 1):
        candidates = score_candidates(db, current, exclude_ids, limit=20)
        if not candidates:
            break
        weights = [max(c[0], 0.1) for c in candidates]
        chosen = random.choices(candidates, weights=weights, k=1)[0][1]
        result.append(chosen)
        exclude_ids.append(chosen["id"])
        current = chosen

    return jsonify({"tracks": result})


# --------------------------------------------------------- smart playlists --
# A saved filter (the same shape _build_track_filter already accepts) that
# re-evaluates against the live library every time it's opened, instead of a
# fixed track list -- "5-star Rock from the 90s" always reflects your
# current ratings and tags rather than a snapshot from when it was made.

@app.route("/api/smart-playlists", methods=["GET"])
def list_smart_playlists():
    db = get_db()
    rows = db.execute("SELECT * FROM smart_playlists ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/smart-playlists", methods=["POST"])
def create_smart_playlist():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    rules = data.get("rules") or {}
    if not name or not isinstance(rules, dict):
        abort(400)
    db = get_db()
    cur = db.execute(
        "INSERT INTO smart_playlists (name, rules, created_at) VALUES (?,?,?)",
        (name, json.dumps(rules), datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid, "name": name, "rules": rules})


@app.route("/api/smart-playlists/<int:playlist_id>", methods=["DELETE"])
def delete_smart_playlist(playlist_id):
    db = get_db()
    db.execute("DELETE FROM smart_playlists WHERE id=?", (playlist_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/smart-playlists/<int:playlist_id>/tracks")
def smart_playlist_tracks(playlist_id):
    db = get_db()
    row = db.execute("SELECT * FROM smart_playlists WHERE id=?", (playlist_id,)).fetchone()
    if not row:
        abort(404)
    rules = json.loads(row["rules"])
    where_sql, params = _build_track_filter(rules)
    sort = rules.get("sort", "artist")
    sort_map = {
        "artist": "t.artist COLLATE NOCASE, t.title COLLATE NOCASE",
        "rating": "r.rating DESC NULLS LAST, t.artist COLLATE NOCASE",
        "year": "t.year DESC, t.artist COLLATE NOCASE",
    }
    order_sql = sort_map.get(sort, sort_map["artist"])
    rows = db.execute(
        f"SELECT {TRACK_FIELDS} FROM tracks t LEFT JOIN ratings r ON r.track_id = t.id "
        f"{where_sql} ORDER BY {order_sql} LIMIT 500",
        params,
    ).fetchall()
    return jsonify({
        "id": row["id"], "name": row["name"], "rules": rules,
        "tracks": [track_to_dict(r) for r in rows],
    })


# ---------------------------------------------------------------- playlists --

@app.route("/api/playlists", methods=["GET"])
def list_playlists():
    db = get_db()
    rows = db.execute(
        "SELECT p.id, p.name, p.created_at, COUNT(pt.track_id) as track_count "
        "FROM playlists p LEFT JOIN playlist_tracks pt ON pt.playlist_id = p.id "
        "GROUP BY p.id ORDER BY p.created_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/playlists", methods=["POST"])
def create_playlist():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip() or f"Playlist {datetime.datetime.now():%Y-%m-%d %H:%M}"
    db = get_db()
    cur = db.execute(
        "INSERT INTO playlists (name, created_at) VALUES (?, ?)",
        (name, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    playlist_id = cur.lastrowid

    track_ids = data.get("track_ids") or []
    for i, tid in enumerate(track_ids):
        db.execute(
            "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?,?,?)",
            (playlist_id, tid, i),
        )
    db.commit()
    return jsonify({"id": playlist_id, "name": name})


@app.route("/api/playlists/<int:playlist_id>")
def get_playlist(playlist_id):
    db = get_db()
    playlist = db.execute("SELECT * FROM playlists WHERE id=?", (playlist_id,)).fetchone()
    if not playlist:
        abort(404)
    rows = db.execute(
        f"SELECT {TRACK_FIELDS}, pt.position FROM playlist_tracks pt "
        f"JOIN tracks t ON t.id = pt.track_id LEFT JOIN ratings r ON r.track_id = t.id "
        f"WHERE pt.playlist_id = ? ORDER BY pt.position",
        (playlist_id,),
    ).fetchall()
    return jsonify({"id": playlist["id"], "name": playlist["name"],
                     "tracks": [track_to_dict(r) for r in rows]})


@app.route("/api/playlists/<int:playlist_id>", methods=["DELETE"])
def delete_playlist(playlist_id):
    db = get_db()
    db.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/playlists/<int:playlist_id>/tracks", methods=["POST"])
def add_playlist_track(playlist_id):
    data = request.get_json(force=True, silent=True) or {}
    track_ids = data.get("track_ids")
    if track_ids is None:
        single = data.get("track_id")
        if not single:
            abort(400)
        track_ids = [single]
    db = get_db()
    pos_row = db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_tracks WHERE playlist_id=?",
        (playlist_id,),
    ).fetchone()
    next_pos = pos_row[0]
    for i, track_id in enumerate(track_ids):
        db.execute(
            "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?,?,?)",
            (playlist_id, track_id, next_pos + i),
        )
    db.commit()
    return jsonify({"ok": True, "added": len(track_ids)})


@app.route("/api/playlists/<int:playlist_id>/tracks/<int:track_id>", methods=["DELETE"])
def remove_playlist_track(playlist_id, track_id):
    db = get_db()
    db.execute(
        "DELETE FROM playlist_tracks WHERE playlist_id=? AND track_id=?",
        (playlist_id, track_id),
    )
    db.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------- export / import --
# Keyed by file path rather than track id, since ids are just SQLite
# autoincrement values -- they won't line up after a rescan or on a
# different machine's copy of the same library, but paths will.

@app.route("/api/export")
def export_library_data():
    db = get_db()
    ratings = db.execute(
        "SELECT t.path, r.rating FROM ratings r JOIN tracks t ON t.id = r.track_id"
    ).fetchall()
    playlists = db.execute("SELECT id, name FROM playlists").fetchall()
    playlist_data = []
    for p in playlists:
        rows = db.execute(
            "SELECT t.path FROM playlist_tracks pt JOIN tracks t ON t.id = pt.track_id "
            "WHERE pt.playlist_id=? ORDER BY pt.position", (p["id"],)
        ).fetchall()
        playlist_data.append({"name": p["name"], "paths": [r["path"] for r in rows]})
    resp = make_response(jsonify({
        "version": 1,
        "exported_at": datetime.datetime.utcnow().isoformat(),
        "ratings": [{"path": r["path"], "rating": r["rating"]} for r in ratings],
        "playlists": playlist_data,
    }))
    resp.headers["Content-Disposition"] = "attachment; filename=jukebox-export.json"
    return resp


@app.route("/api/import", methods=["POST"])
def import_library_data():
    data = request.get_json(force=True, silent=True) or {}
    db = get_db()
    _snapshot_db()

    ratings_applied = 0
    for r in data.get("ratings", []):
        row = db.execute("SELECT id FROM tracks WHERE path=?", (r.get("path"),)).fetchone()
        if row and isinstance(r.get("rating"), int):
            db.execute(
                "INSERT INTO ratings (track_id, rating, rated_at) VALUES (?,?,?) "
                "ON CONFLICT(track_id) DO UPDATE SET rating=excluded.rating, rated_at=excluded.rated_at",
                (row["id"], r["rating"], datetime.datetime.utcnow().isoformat()),
            )
            ratings_applied += 1

    playlists_created = 0
    for p in data.get("playlists", []):
        cur = db.execute(
            "INSERT INTO playlists (name, created_at) VALUES (?, ?)",
            (p.get("name") or "Imported playlist", datetime.datetime.utcnow().isoformat()),
        )
        playlist_id = cur.lastrowid
        pos = 0
        for path in p.get("paths", []):
            row = db.execute("SELECT id FROM tracks WHERE path=?", (path,)).fetchone()
            if row:
                db.execute(
                    "INSERT OR IGNORE INTO playlist_tracks (playlist_id, track_id, position) VALUES (?,?,?)",
                    (playlist_id, row["id"], pos),
                )
                pos += 1
        playlists_created += 1

    db.commit()
    return jsonify({"ok": True, "ratings_applied": ratings_applied, "playlists_created": playlists_created})


# ------------------------------------------------------------ internet radio --
# Backed by Radio Browser (radio-browser.info), a free, open directory of
# public station streams and their metadata (tags, country, popularity
# votes/clicks) -- this app never hosts or caches any station's audio, it
# only looks up and plays each station's own publicly broadcast stream URL,
# same as tuning a physical radio or a browser hitting the URL directly, so
# there's no copyright exposure on our side; each station is responsible for
# its own licensing.
RADIO_UA = "NotoriousBPM/1.0"

# GUI-launched apps on macOS (double-clicked from Finder/Dock, or opened via
# `open`) don't reliably inherit the same PATH a Terminal shell has --
# Homebrew's install location is a common casualty of this, so a tool that's
# genuinely installed and on PATH in Terminal can still come up "missing"
# here via a bare shutil.which(). Checking these well-known install
# locations directly as a fallback covers that without requiring the user
# to do anything about their shell environment.
_COMMON_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin")


def _find_binary(name):
    found = shutil.which(name)
    if found:
        return found
    for d in _COMMON_BIN_DIRS:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


RADIO_FALLBACK_HOSTS = (
    "de1.api.radio-browser.info",
    "de2.api.radio-browser.info",
    "nl1.api.radio-browser.info",
    "at1.api.radio-browser.info",
)

# Radio Browser's raw tag list runs to tens of thousands of mostly noisy,
# user-submitted values (duplicates, typos, non-genre tags) -- a curated
# list makes for a usable dropdown; each value is still just passed straight
# through as their `tag` search param, so it's not a hardcoded ID of
# anything on their end that could later disappear.
RADIO_GENRES = (
    "pop", "rock", "jazz", "classical", "electronic", "hip hop", "dance",
    "country", "reggae", "metal", "blues", "folk", "indie", "house",
    "techno", "ambient", "oldies", "latin", "disco", "punk", "soul", "funk",
    "world", "news", "talk", "sports", "k-pop", "chillout", "lounge", "gospel",
)

_radio_cache = {"countries": None, "countries_ts": 0}
RADIO_CACHE_TTL = 24 * 3600


def _radio_hosts():
    """Live mirror hostnames via all.api.radio-browser.info's round-robin
    DNS, shuffled so repeated calls spread across mirrors instead of
    hammering whichever sorts first -- falls back to a small static list if
    DNS resolution itself fails (offline, or that domain is unreachable)."""
    try:
        _, _, ips = socket.gethostbyname_ex("all.api.radio-browser.info")
        hosts, seen = [], set()
        for ip in ips:
            try:
                host = socket.gethostbyaddr(ip)[0]
            except socket.herror:
                continue
            if host not in seen:
                seen.add(host)
                hosts.append(host)
        random.shuffle(hosts)
        return hosts or list(RADIO_FALLBACK_HOSTS)
    except socket.gaierror:
        return list(RADIO_FALLBACK_HOSTS)


def _radio_get(path, params=None, timeout=8):
    """Tries live mirrors in turn until one answers -- individual Radio
    Browser mirrors go down or rotate over time, so hardcoding a single one
    would eventually just break."""
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    last_err = None
    for host in _radio_hosts()[:5]:
        url = f"https://{host}{path}{query}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": RADIO_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Couldn't reach the radio directory ({last_err}).")


@app.route("/api/radio/genres")
def radio_genres():
    return jsonify(list(RADIO_GENRES))


@app.route("/api/radio/countries")
def radio_countries():
    now = time.time()
    if _radio_cache["countries"] is None or now - _radio_cache["countries_ts"] > RADIO_CACHE_TTL:
        try:
            raw = _radio_get("/json/countries", {"order": "name"})
            # Radio Browser's raw list carries the same country twice under
            # differently-cased ISO codes for a handful of entries (e.g. "US"
            # and "us") -- collapse by uppercased code, keeping whichever
            # duplicate has more stations, so the dropdown doesn't show the
            # same country name side by side twice.
            by_code = {}
            for c in raw:
                code = (c.get("iso_3166_1") or "").upper()
                if not code or not c.get("name") or not c.get("stationcount", 0) > 0:
                    continue
                if code not in by_code or c["stationcount"] > by_code[code]["count"]:
                    by_code[code] = {"code": code, "name": c["name"], "count": c["stationcount"]}
            countries = sorted(by_code.values(), key=lambda c: c["name"])
            _radio_cache["countries"] = countries
            _radio_cache["countries_ts"] = now
        except RuntimeError:
            if _radio_cache["countries"] is None:
                return jsonify({"ok": False, "error": "Couldn't reach the radio directory.", "countries": []}), 502
    return jsonify({"ok": True, "countries": _radio_cache["countries"]})


def _format_stations(raw):
    return [
        {
            "uuid": s.get("stationuuid"),
            "name": s.get("name") or "Unnamed station",
            "url": s.get("url_resolved") or s.get("url"),
            "favicon": s.get("favicon") or None,
            "country": s.get("country") or None,
            "tags": s.get("tags") or None,
            "bitrate": s.get("bitrate") or None,
            "votes": s.get("votes"),
            "clickcount": s.get("clickcount"),
        }
        for s in raw
        if s.get("url_resolved") or s.get("url")
    ]


@app.route("/api/radio/stations")
def radio_stations():
    genre = (request.args.get("genre") or "").strip()
    country_code = (request.args.get("country") or "").strip()
    sort = request.args.get("sort", "votes")
    try:
        limit = min(max(int(request.args.get("limit", 60)), 1), 200)
    except ValueError:
        limit = 60

    params = {
        "order": sort if sort in ("votes", "clickcount", "bitrate", "name") else "votes",
        "reverse": "true",
        "limit": limit,
        "hidebroken": "true",
    }
    if genre:
        params["tag"] = genre
    if country_code:
        params["countrycode"] = country_code

    try:
        raw = _radio_get("/json/stations/search", params)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e), "stations": []}), 502

    return jsonify({"ok": True, "stations": _format_stations(raw)})


# A library genre can be free-text and often compound ("Rap/Hip Hop",
# "Chill Out/Trip-Hop/Lounge") -- this maps it to the closest tag in our own
# curated RADIO_GENRES list rather than passing it straight through to
# Radio Browser's tag search, where the exact library string would usually
# match nothing at all.
_GENRE_MATCH_ALIASES = {
    "rap": "hip hop", "r&b": "soul", "rnb": "soul", "trip-hop": "chillout",
    "trip hop": "chillout", "edm": "electronic", "alternative": "indie",
    "singer-songwriter": "folk", "orchestral": "classical", "afrobeat": "world",
    "african": "world", "asian": "world", "worldbeat": "world",
}


def _match_radio_genre(library_genre):
    norm = library_genre.lower()
    for tag in RADIO_GENRES:
        if tag in norm or norm in tag:
            return tag
    for key, tag in _GENRE_MATCH_ALIASES.items():
        if key in norm:
            return tag
    return None


@app.route("/api/radio/match")
def radio_match():
    """"Because your library leans on X, here's live radio playing similar
    music" -- takes the library's own most-common genres (by track count),
    maps the first one with a usable match to one of our curated radio tags,
    and searches Radio Browser with it. Falls through to the next
    most-common library genre if one has no reasonable radio-tag match."""
    db = get_db()
    rows = db.execute(
        "SELECT primary_genre, COUNT(*) as cnt FROM tracks "
        "WHERE primary_genre IS NOT NULL AND primary_genre != '' "
        "GROUP BY primary_genre ORDER BY cnt DESC LIMIT 15"
    ).fetchall()

    matched_tag = None
    matched_genre = None
    for row in rows:
        tag = _match_radio_genre(row["primary_genre"])
        if tag:
            matched_tag = tag
            matched_genre = row["primary_genre"]
            break

    if not matched_tag:
        return jsonify({"ok": False, "error": "no_match"})

    params = {"order": "votes", "reverse": "true", "limit": 30, "hidebroken": "true", "tag": matched_tag}
    try:
        raw = _radio_get("/json/stations/search", params)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 502

    return jsonify({
        "ok": True, "library_genre": matched_genre, "matched_tag": matched_tag,
        "stations": _format_stations(raw),
    })


@app.route("/api/radio/click/<uuid>", methods=["POST"])
def radio_click(uuid):
    # Purely a popularity signal back to Radio Browser (their vote/click
    # counters, used for the "sort by popularity" option) -- never lets a
    # failure here interrupt playback, which has already started against
    # the URL the frontend already has.
    try:
        _radio_get(f"/json/url/{uuid}")
    except Exception:
        pass
    return jsonify({"ok": True})


# Latest parsed "Artist - Title" (from ICY metadata, see radio_proxy below)
# for each in-flight proxied stream, keyed by a random id the frontend mints
# per play -- lets /api/radio/now-playing answer "what's on air right now"
# without the frontend needing any access to the raw byte stream itself.
_radio_now_playing = {}
_radio_now_playing_lock = threading.Lock()
_ICY_TITLE_RE = re.compile(r"StreamTitle='([^']*)'")


@app.route("/api/radio/proxy")
def radio_proxy():
    """Proxies a station's live stream through this server instead of
    letting the browser hit it directly. Two independent reasons this
    exists, both requiring the same fix:

    1. Web Audio's AnalyserNode (what drives the Hi-Fi theme's VU meters)
       silently returns all-zero data for cross-origin audio unless the
       remote server sends CORS headers -- virtually no Icecast/Shoutcast
       radio stream does. Proxied through here, the browser sees a
       same-origin URL, so the analyser gets real samples. Playback itself
       never needed this (a plain <audio src> to the external URL plays
       fine); only the visualizer tap does.
    2. The actual "now playing" artist/track for a station is carried as
       ICY metadata interleaved *inside* the audio byte stream itself
       (a `StreamTitle='...'` block every `icy-metaint` bytes) -- there's no
       way for client-side JS to see that at all from a direct <audio src>,
       since browsers parse and discard it silently. Reading the stream
       here lets us pull each StreamTitle out, strip it back out of the
       bytes actually sent to the browser (leaving pure audio, since
       feeding the raw interleaved bytes to <audio> would corrupt playback),
       and hand the parsed title to /api/radio/now-playing to poll.
    """
    url = request.args.get("url", "")
    sid = request.args.get("sid", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        abort(400)

    req = urllib.request.Request(url, headers={"User-Agent": RADIO_UA, "Icy-MetaData": "1"})
    try:
        upstream = urllib.request.urlopen(req, timeout=10)
    except Exception:
        abort(502)

    content_type = upstream.headers.get("Content-Type", "audio/mpeg")
    try:
        metaint = int(upstream.headers.get("icy-metaint", 0))
    except ValueError:
        metaint = 0

    def generate():
        try:
            if metaint <= 0:
                # Station doesn't support ICY metadata -- still worth
                # proxying for the CORS/VU-meter fix, just with no title.
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    yield chunk
                return

            while True:
                audio_chunk = upstream.read(metaint)
                if not audio_chunk:
                    break
                yield audio_chunk

                len_byte = upstream.read(1)
                if not len_byte:
                    break
                meta_len = len_byte[0] * 16
                if meta_len:
                    meta = upstream.read(meta_len).rstrip(b"\x00").decode("utf-8", "ignore")
                    m = _ICY_TITLE_RE.search(meta)
                    if m and sid:
                        with _radio_now_playing_lock:
                            _radio_now_playing[sid] = m.group(1).strip()
        finally:
            upstream.close()
            if sid:
                with _radio_now_playing_lock:
                    _radio_now_playing.pop(sid, None)

    return Response(generate(), mimetype=content_type)


@app.route("/api/radio/now-playing")
def radio_now_playing():
    sid = request.args.get("sid", "")
    with _radio_now_playing_lock:
        title = _radio_now_playing.get(sid)
    return jsonify({"title": title})


@app.route("/api/radio/acoustid-key")
def get_acoustid_key():
    return jsonify({"acoustidApiKey": jukebox_config.load_config().get("acoustidApiKey") or ""})


@app.route("/api/radio/acoustid-key", methods=["POST"])
def set_acoustid_key():
    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("acoustidApiKey") or "").strip()
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("acoustidApiKey", key))
    return jsonify({"ok": True})


@app.route("/api/radio/identify", methods=["POST"])
def radio_identify():
    """Shazam-style "what's this song" for a station whose ICY metadata is
    missing, empty, or just repeats the station's own name/slogan.
    Fingerprints ~15s of live audio with Chromaprint (fpcalc) and looks it
    up against AcoustID -- the same free, open, MusicBrainz-backed
    fingerprint database tools like Picard use. No per-query cost, nothing
    stored; the captured clip is a temp file deleted right after."""
    # These "soft" failures all return HTTP 200 -- the frontend needs to
    # tell them apart (missing key vs missing binary vs no match) to show
    # the right message/prompt, and api()'s fetch wrapper throws away the
    # JSON body entirely for any non-2xx response, same convention already
    # used by /api/choose-folder's {"ok": false, "cancelled": true}.
    fpcalc_path = _find_binary("fpcalc")
    if not fpcalc_path:
        return jsonify({"ok": False, "error": "fpcalc_missing"})

    api_key = (jukebox_config.load_config().get("acoustidApiKey") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "no_api_key"})

    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        abort(400)

    tmp_path = None
    try:
        # Plain capture, no Icy-MetaData request -- fpcalc needs a clean,
        # uninterrupted clip; the ICY "now playing" title (if any) is
        # handled entirely separately by the live proxy above. Bounded by
        # wall-clock time (not byte count) so a ~15s clip comes out right
        # regardless of the station's bitrate.
        req = urllib.request.Request(url, headers={"User-Agent": RADIO_UA})
        with urllib.request.urlopen(req, timeout=10) as upstream:
            with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
                tmp_path = tmp.name
                start = time.time()
                total = 0
                while time.time() - start < 15 and total < 5_000_000:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    tmp.write(chunk)
                    total += len(chunk)

        result = subprocess.run(
            [fpcalc_path, "-json", tmp_path], capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0 or not result.stdout:
            return jsonify({"ok": False, "error": "Couldn't read enough audio from the stream to identify it."})
        fp_data = json.loads(result.stdout)

        params = {
            "client": api_key,
            "fingerprint": fp_data["fingerprint"],
            "duration": int(fp_data["duration"]),
            "meta": "recordings",
        }
        lookup_url = "https://api.acoustid.org/v2/lookup?" + urllib.parse.urlencode(params)
        lookup_req = urllib.request.Request(lookup_url, headers={"User-Agent": RADIO_UA})
        with urllib.request.urlopen(lookup_req, timeout=10) as resp:
            lookup = json.loads(resp.read().decode("utf-8"))

        if lookup.get("status") != "ok":
            msg = (lookup.get("error") or {}).get("message", "Lookup failed")
            return jsonify({"ok": False, "error": msg})

        # A single clip can match several near-identical AcoustID entries
        # (different pressings/remasters of the same recording) -- keep
        # whichever has the highest confidence score.
        best = None
        for r in lookup.get("results", []):
            for rec in r.get("recordings", []):
                if rec.get("title") and rec.get("artists"):
                    score = r.get("score", 0)
                    if not best or score > best[0]:
                        best = (score, rec)
        if not best:
            return jsonify({"ok": False, "error": "no_match"})

        score, rec = best
        artist = ", ".join(a["name"] for a in rec.get("artists", []) if a.get("name"))
        return jsonify({"ok": True, "artist": artist, "title": rec["title"], "score": score})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    finally:
        if tmp_path and os.path.isfile(tmp_path):
            os.remove(tmp_path)


# Server-side VU/spectrum levels for internet radio. Web Audio's
# AnalyserNode -- what normally drives the Hi-Fi needle and Cassette
# spectrum bars -- turns out to have a real, long-standing WebKit bug
# (bugs.webkit.org #180696, #211394 and others) where createMediaElementSource
# never produces usable data for network-streamed audio at all, no matter
# how or when it's set up; local file playback is unaffected, which is
# exactly the split reported. Rather than rely on the browser to analyze a
# live stream, ffmpeg decodes a second, independent connection to the same
# station to raw PCM here, and the frontend polls for the numbers instead of
# reading them out of an AnalyserNode.
RADIO_LEVELS_BANDS = 12
RADIO_LEVELS_WINDOW = 256  # samples per FFT window
_radio_levels_state = {}
_radio_levels_lock = threading.Lock()


def _fft(x):
    n = len(x)
    if n <= 1:
        return x
    even = _fft(x[0::2])
    odd = _fft(x[1::2])
    combined = [0j] * n
    for k in range(n // 2):
        t = cmath.exp(-2j * cmath.pi * k / n) * odd[k]
        combined[k] = even[k] + t
        combined[k + n // 2] = even[k] - t
    return combined


def _kill_ffmpeg(proc):
    """proc.terminate() (SIGTERM) alone reliably leaves this ffmpeg running
    -- it's normally blocked writing to a stdout pipe nobody's draining
    anymore by the time this is called, and doesn't act on SIGTERM promptly
    from that state. There's nothing worth flushing gracefully in a
    throwaway analysis feed, so go straight to SIGKILL."""
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


def _radio_levels_worker(sid, url, ffmpeg_path):
    proc = None
    try:
        proc = subprocess.Popen(
            [ffmpeg_path, "-user_agent", RADIO_UA, "-i", url,
             "-f", "s16le", "-ar", "22050", "-ac", "1", "-loglevel", "quiet", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        with _radio_levels_lock:
            if sid not in _radio_levels_state:
                _kill_ffmpeg(proc)
                return
            _radio_levels_state[sid]["process"] = proc

        chunk_bytes = 22050 * 2 // 10  # ~100ms of mono 16-bit PCM
        while True:
            with _radio_levels_lock:
                entry = _radio_levels_state.get(sid)
                if not entry:
                    break
                # A client that stopped polling (closed the app, switched
                # away without the stop call reaching us) shouldn't leave
                # ffmpeg running forever -- self-terminate once idle.
                if time.time() - entry["last_poll"] > 15:
                    break

            t0 = time.time()
            data = proc.stdout.read(chunk_bytes)
            if not data or len(data) < 4:
                break
            n = len(data) // 2
            samples = struct.unpack(f"<{n}h", data[:n * 2])

            rms = min(1.0, (sum(s * s for s in samples) / len(samples)) ** 0.5 / 32768)

            window = list(samples[:RADIO_LEVELS_WINDOW]) + [0] * max(0, RADIO_LEVELS_WINDOW - len(samples))
            mags = [abs(v) for v in _fft(window)[:RADIO_LEVELS_WINDOW // 2]]
            peak = max(mags) or 1.0
            per_band = len(mags) // RADIO_LEVELS_BANDS
            bands = [
                min(1.0, sum(mags[i * per_band:(i + 1) * per_band]) / per_band / peak)
                for i in range(RADIO_LEVELS_BANDS)
            ]

            with _radio_levels_lock:
                if sid not in _radio_levels_state:
                    break
                _radio_levels_state[sid]["level"] = rms
                _radio_levels_state[sid]["bands"] = bands

            elapsed = time.time() - t0
            if elapsed < 0.1:
                time.sleep(0.1 - elapsed)
    except Exception:
        pass
    finally:
        with _radio_levels_lock:
            _radio_levels_state.pop(sid, None)
        if proc:
            _kill_ffmpeg(proc)


@app.route("/api/radio/levels/start", methods=["POST"])
def radio_levels_start():
    ffmpeg_path = _find_binary("ffmpeg")
    if not ffmpeg_path:
        return jsonify({"ok": False, "error": "ffmpeg_missing"})

    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "")
    sid = data.get("sid", "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not sid:
        abort(400)

    with _radio_levels_lock:
        # Single-active-station app -- stop any other running level workers
        # so switching stations can't leak ffmpeg processes.
        for old_sid, entry in list(_radio_levels_state.items()):
            if old_sid == sid:
                continue
            proc = entry.get("process")
            if proc:
                _kill_ffmpeg(proc)
            _radio_levels_state.pop(old_sid, None)

        if sid in _radio_levels_state:
            return jsonify({"ok": True})
        _radio_levels_state[sid] = {
            "level": 0.0, "bands": [0.0] * RADIO_LEVELS_BANDS,
            "process": None, "last_poll": time.time(),
        }

    threading.Thread(target=_radio_levels_worker, args=(sid, url, ffmpeg_path), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/radio/levels")
def radio_levels():
    sid = request.args.get("sid", "")
    with _radio_levels_lock:
        entry = _radio_levels_state.get(sid)
        if not entry:
            return jsonify({"ok": False})
        entry["last_poll"] = time.time()
        return jsonify({"ok": True, "level": entry["level"], "bands": entry["bands"]})


@app.route("/api/radio/levels/stop", methods=["POST"])
def radio_levels_stop():
    data = request.get_json(force=True, silent=True) or {}
    sid = data.get("sid", "")
    with _radio_levels_lock:
        entry = _radio_levels_state.pop(sid, None)
    if entry and entry.get("process"):
        _kill_ffmpeg(entry["process"])
    return jsonify({"ok": True})


@app.route("/api/radio/levels/stop-all", methods=["POST"])
def radio_levels_stop_all():
    """Called from the desktop shell's window-closing handler, alongside
    the existing auto-backup call -- a normal app quit while radio is
    playing would otherwise leave its ffmpeg decode running until the 15s
    idle timeout catches it on its own."""
    with _radio_levels_lock:
        entries = list(_radio_levels_state.values())
        _radio_levels_state.clear()
    for entry in entries:
        if entry.get("process"):
            _kill_ffmpeg(entry["process"])
    return jsonify({"ok": True})


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    if not os.path.isfile(DB_PATH):
        print("No library.db found — run scan_library.py first.")
    debug = os.environ.get("JUKEBOX_DEBUG") == "1"
    port = int(os.environ.get("JUKEBOX_PORT", "5151"))
    app.run(host="127.0.0.1", port=port, debug=debug, threaded=True)
