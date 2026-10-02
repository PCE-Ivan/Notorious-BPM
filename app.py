#!/usr/bin/env python3
import cmath
import io
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
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
import urllib.request

from flask import Flask, g, jsonify, request, send_file, abort, send_from_directory, make_response, Response

import config as jukebox_config
import health
import jobs
import journal
import logging
import logging_setup
import system_status
import tagio
from werkzeug.exceptions import HTTPException
from fs_safety import safe_move
import library_manager

# Before resolve_startup() on purpose: see logging_setup's own docstring --
# a JUKEBOX_DB_PATH already set at this point is a dev/test override, and
# the log must follow that, not the real library resolve_startup() picks.
logging_setup.setup_logging()
log = logging.getLogger("jukebox.app")

# Must run before MUSIC_DIR/DB_PATH below are computed -- resolves which
# library is "current" (running the one-time legacy migration the first
# time this runs post-update) and sets JUKEBOX_DB_PATH/JUKEBOX_MUSIC_DIR
# accordingly, so both this module's own globals just below AND every
# other module's identical os.environ.get("JUKEBOX_DB_PATH", ...)-based
# globals (scan_library.py, organize_by_artist.py, fill_genres.py, etc --
# all already re-read via the importlib.reload() calls sprinkled through
# this file before each use) resolve to the right library from the very
# first request, with no changes needed in any of those other modules.
library_manager.resolve_startup()

MUSIC_DIR = jukebox_config.get_music_dir()
DEFAULT_DB_PATH = os.path.join(jukebox_config.get_app_data_dir(), "library.db")
DB_PATH = os.environ.get("JUKEBOX_DB_PATH", DEFAULT_DB_PATH)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# Fixes a real bug that predates this call: ART_CACHE_DIR/TRASH_DIR/
# BACKUP_DIR below used to be computed as plain os.path.dirname(DB_PATH)
# subfolders, ignoring library_manager's own companion_dir() -- unlike
# _switch_library() further down, which always used it correctly. Moves
# anything already sitting in the old, wrong location into the right one
# first, so this library's real trash/art cache/backups aren't orphaned
# by the fix. See migrate_stray_companion_dir's own docstring.
library_manager.migrate_stray_companion_dir(DB_PATH)

_companion_dir = library_manager.companion_dir(DB_PATH)
ART_CACHE_DIR = os.path.join(_companion_dir, "art_cache")
os.makedirs(ART_CACHE_DIR, exist_ok=True)

TRASH_DIR = os.path.join(_companion_dir, "trash")
BACKUP_DIR = os.path.join(_companion_dir, "backups")

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
    databases from before this feature don't need a manual migration.

    Also the one place that guarantees the base schema exists at all
    before this app answers a single request -- in normal desktop use
    launcher.py runs a real scan_library.scan() right after the first-run
    folder picker, which creates `tracks` (via scan_library.build_schema)
    itself, so every route here has always found it already there in
    practice. But that's true only because of that specific boot order,
    not because anything here checks for it: any other way of starting
    this Flask app (a test, a future boot path) hits `no such table:
    tracks` on literally the first request. Calling scan_library's own
    build_schema() -- the exact function scan() itself calls, language
    column and all -- closes that gap for good instead of depending on
    one launch script always running first, and can never drift from it
    the way a hand-copied duplicate of the same CREATE TABLE statements
    eventually would.
    """
    import scan_library
    scan_library.build_schema(db)

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

PLAYLIST_NAME_MAX_LEN = 200
SMART_PLAYLIST_NAME_MAX_LEN = 200


# ---------------------------------------------------------------- browsing --

# Guards switching to a different library against every background job
# below, in both directions: _switch_library holds this for its entire
# duration and refuses to run while any job is active, AND every job's own
# _start_*_bg function below also takes this (briefly, just around its own
# "check + flip running=True" step) before starting -- so a switch can only
# ever complete when nothing is running, and nothing new can start while a
# switch is in progress. That two-way guarantee matters because several of
# the _run_*_bg thread bodies below re-read the module-level MUSIC_DIR/
# DB_PATH globals fresh at multiple points *during* a job that can run for
# minutes, not just once at thread-start -- a one-directional "no jobs
# running" check alone wouldn't stop a job from starting in the instant
# after that check and then reading a library out from under a switch that
# lands mid-job. _any_background_job_running() (defined here, referencing
# the state dicts declared further down -- fine in Python, since it's only
# ever called after the whole module has loaded) is every one of this
# file's 13 job-state dicts' "running" flag.
_library_lock = threading.Lock()


def _any_background_job_running():
    # Every Job registers itself in jobs.REGISTRY, so a job added later is
    # covered automatically -- the hand-maintained tuple this replaced had to
    # be remembered (and was, once, forgotten) each time a new one appeared.
    return jobs.any_running()


@app.route("/api/jobs")
def jobs_list():
    """Activity tray: every running job plus anything finished recently."""
    return jsonify(jobs.list_jobs())


@app.route("/api/jobs/<name>/cancel", methods=["POST"])
def jobs_cancel(name):
    job = jobs.REGISTRY.get(name)
    if not job:
        abort(404)
    return jsonify({"ok": job.cancel()})


@app.route("/api/jobs/<name>/dismiss", methods=["POST"])
def jobs_dismiss(name):
    job = jobs.REGISTRY.get(name)
    if not job:
        abort(404)
    job.dismiss()
    return jsonify({"ok": True})


# ------------------------------------------------- errors & diagnostics --

@app.errorhandler(Exception)
def _handle_unexpected_error(e):
    """Anything that escapes a route used to become Flask's bare HTML
    "500 Internal Server Error" -- no log, no explanation (that's exactly
    how a macOS folder-permission block looked). Logged with its traceback
    and returned as JSON the front end can show as readable text."""
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled error on %s %s", request.method, request.path)
    kind, message = system_status.describe_exception(e, DB_PATH, MUSIC_DIR)
    return jsonify({"ok": False, "error": message, "kind": kind}), 500


@app.route("/api/system/status")
def system_status_route():
    problems = system_status.library_problems(DB_PATH, MUSIC_DIR)
    return jsonify({"ok": not problems, "problems": problems})


@app.route("/api/system/open-privacy-settings", methods=["POST"])
def system_open_privacy_settings():
    return jsonify({"ok": system_status.open_privacy_settings()})


@app.route("/api/system/reveal-log", methods=["POST"])
def system_reveal_log():
    path = logging_setup.log_path()
    if not path or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "No log file yet"})
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path])
    return jsonify({"ok": True, "path": path})


@app.route("/api/diagnostics")
def diagnostics():
    """Everything worth pasting into a bug report, in one place."""
    def redact(cfg):
        return {
            k: ("(set)" if v else "(empty)") if any(w in k.lower() for w in ("key", "token", "secret")) else v
            for k, v in cfg.items()
        }

    try:
        track_count = get_db().execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    except Exception as e:
        track_count = f"unavailable ({e.__class__.__name__})"
    info = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "library": DB_PATH, "music_dir": MUSIC_DIR,
        "art_cache": ART_CACHE_DIR, "trash": TRASH_DIR, "backups": BACKUP_DIR,
        "tracks": track_count,
        "tools": {"ffmpeg": _find_binary("ffmpeg"), "fpcalc": _find_binary("fpcalc")},
        "config": redact(jukebox_config.load_config()),
        "problems": system_status.library_problems(DB_PATH, MUSIC_DIR),
        "jobs": jobs.list_jobs(),
        "log_file": logging_setup.log_path(),
    }
    lines = [f"{k}: {v}" for k, v in info.items()]
    return jsonify({"info": info, "text": "\n".join(lines) + "\n\n--- recent log ---\n" + logging_setup.tail(20000)})


# ------------------------------------------------------------ library health --
_health_job = jobs.Job(
    "health_check", "Checking library health",
    summarize=lambda s: ("no problems found" if (s["result"] or {}).get("healthy") else
                         "{n} thing(s) to review".format(n=len((s["result"] or {}).get("issues", [])))),
)
_health_state = _health_job.state
_HEALTH_REPAIRS = {
    "remove_missing_tracks", "delete_orphan_art", "clear_stale_none", "reset_art_flags",
    "set_art_flags", "delete_dangling_trash", "delete_dangling_links", "migrate_stray_dirs",
}


def _embedded_art_present(path):
    # A file we can't open is treated as "has art" -- the health check must
    # never turn an unreadable file into a claim about its contents.
    return _read_raw_tag_presence(path)[2] if os.path.isfile(path) else True


def _run_health_bg():
    report = health.run_checks(
        DB_PATH, MUSIC_DIR, ART_CACHE_DIR, TRASH_DIR, BACKUP_DIR,
        stray_dir=os.path.dirname(DB_PATH), correct_dir=library_manager.companion_dir(DB_PATH),
        has_embedded_art=_embedded_art_present, progress=_health_job.progress,
    )
    _health_state["result"] = report
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("last_health_check_at", time.time()))


@app.route("/api/health/check", methods=["POST"])
def health_check_route():
    if not _health_job.start(_run_health_bg, guard=_library_lock):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True})


@app.route("/api/health/progress")
def health_progress():
    return jsonify(_health_state)


@app.route("/api/health/last")
def health_last():
    return jsonify({
        "result": _health_state.get("result"),
        "last_check_at": jukebox_config.load_config().get("last_health_check_at"),
    })


@app.route("/api/health/repair", methods=["POST"])
def health_repair_route():
    data = request.get_json(force=True, silent=True) or {}
    repairs = [r for r in (data.get("repairs") or []) if r in _HEALTH_REPAIRS]
    if not repairs:
        return jsonify({"ok": False, "error": "Nothing selected to repair."})
    if _any_background_job_running():
        return jsonify({"ok": False, "error": "A background task is running — wait for it to finish, then try again."})
    with _library_lock:
        _snapshot_db()
        close_db(None)
        results = health.repair(
            repairs, DB_PATH, MUSIC_DIR, ART_CACHE_DIR, TRASH_DIR,
            has_embedded_art=_embedded_art_present,
            migrate_stray=lambda: library_manager.migrate_stray_companion_dir(DB_PATH),
        )
    log.info("Health repair applied: %s", results)
    return jsonify({"ok": True, "results": results})


# ------------------------------------------------------ change history / undo --
_undo_job = jobs.Job(
    "undo", "Undoing a change",
    summarize=lambda s: "restored {r}, skipped {k} (changed since), {e} failed".format(
        r=(s["result"] or {}).get("restored", 0), k=(s["result"] or {}).get("skipped", 0),
        e=(s["result"] or {}).get("errors", 0)),
)
_undo_state = _undo_job.state


def _run_undo_bg(op_id):
    _undo_state["result"] = journal.undo(DB_PATH, MUSIC_DIR, op_id, progress=_undo_job.progress)


@app.route("/api/history")
def history_list():
    return jsonify(journal.list_operations(DB_PATH))


@app.route("/api/history/<int:op_id>/undo", methods=["POST"])
def history_undo(op_id):
    if not _undo_job.start(
        _run_undo_bg, op_id, guard=_library_lock, prepare=lambda: (_snapshot_db(), close_db(None)),
    ):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True})


@app.route("/api/history/progress")
def history_progress():
    return jsonify(_undo_state)


# Shared by /api/rescan and /api/choose-folder -- both ultimately just run
# scan_library.scan() over MUSIC_DIR, the only difference being whether the
# folder itself changed first. A full scan of a large library (tens of
# thousands of files, as a freshly-connected drive full of lossless FLACs
# can easily be) took minutes with the old synchronous-request version of
# this, during which the UI had no way to distinguish "still working" from
# "hung" -- there was no progress endpoint to poll, unlike fill-genres/
# fill-years/duplicate-cleanup, which already use this exact pattern.
_scan_job = jobs.Job(
    "scan", "Scanning library",
    summarize=lambda s: "{n} new, {u} updated, {r} removed".format(
        n=(s["result"] or {}).get("inserted", 0), u=(s["result"] or {}).get("updated", 0),
        r=(s["result"] or {}).get("removed", 0)),
)
_scan_state = _scan_job.state


def _run_scan_bg(force_prune=False):
    try:
        import importlib
        import scan_library
        importlib.reload(scan_library)
        stats = scan_library.scan(progress_cb=_scan_job.progress, force_prune=force_prune)
        _scan_state["result"] = stats
        # Recorded on every successful scan regardless of trigger (Rescan
        # button, Choose Folder, or the iPod import's chained rescan) --
        # read back by /api/last-scan so a caller can skip a redundant
        # whole-library rescan it doesn't actually need right now.
        jukebox_config.update_config(lambda cfg: cfg.__setitem__("last_scan_at", datetime.datetime.utcnow().isoformat()))
    finally:
        _invalidate_dup_plan_cache()


def _start_scan_bg(force_prune=False):
    """Returns False (and starts nothing) if a scan is already running --
    same "already running" convention as fill-genres/fill-years, not an
    error, just something the caller can tell the user."""
    return _scan_job.start(_run_scan_bg, force_prune, guard=_library_lock, prepare=lambda: close_db(None))


@app.route("/api/rescan", methods=["POST"])
def rescan():
    _snapshot_db()
    data = request.get_json(force=True, silent=True) or {}
    started = _start_scan_bg(force_prune=bool(data.get("force_prune")))
    return jsonify({"started": started, "error": None if started else "Already running"})


@app.route("/api/scan-progress")
def scan_progress():
    return jsonify(_scan_state)


@app.route("/api/last-scan")
def last_scan():
    """When the library was last actually scanned (any trigger -- Rescan,
    Choose Folder, or an iPod import's chained rescan), so a caller can
    decide a fresh one isn't needed right now instead of always redoing a
    whole-library walk that can take minutes."""
    cfg = jukebox_config.load_config()
    last_scan_at = cfg.get("last_scan_at")
    seconds_ago = None
    if last_scan_at:
        try:
            then = datetime.datetime.fromisoformat(last_scan_at)
            seconds_ago = (datetime.datetime.utcnow() - then).total_seconds()
        except ValueError:
            last_scan_at = None
    return jsonify({"last_scan_at": last_scan_at, "seconds_ago": seconds_ago})


# iPod import flow: copy to a holding folder first (ipod_import.STAGING_DIRNAME,
# under MUSIC_DIR so the eventual move to the library is a fast same-volume
# rename, not a slow cross-device copy) -- then the user reviews the batch,
# optionally fixes names/tags/art in place, and only then moves it into the
# real library as a separate explicit step. Staged tracks are never inserted
# into the tracks table (see ipod_import.py's own module docstring for why);
# every route below reads/writes the staged files directly. Same async-job-
# with-progress-polling shape as the scan above throughout, since each of
# these steps can take minutes over a few thousand tracks and must never
# block a request while running. Classic (clickwheel) iPods only -- they
# mount as a plain disk and store ordinary DRM-free audio files; an iPod
# Touch exposes no such filesystem. See ipod_import.py for how a track's
# real name is recovered from the iPod's own iTunesDB.
_ipod_job = jobs.Job("ipod_import", "Importing from iPod", ipod_name=None)
_ipod_state = _ipod_job.state


def _run_ipod_import_bg(mount, ipod_name, staging_root, existing_index):
    # No importlib.reload here (unlike scan/organize above) -- reloading
    # this specific module (the one that also imports organize_by_artist)
    # deadlocks in the packaged/frozen build specifically, verified
    # against a real device: the process sits at 0% CPU indefinitely,
    # not slow, genuinely stuck. Reload only ever existed so editing
    # this file didn't need an app restart during development; a
    # shipped, frozen build's code never changes at runtime anyway, so
    # a plain import (returning the already-loaded module) loses
    # nothing real here.
    import ipod_import
    stats = ipod_import.import_tracks(
        mount, staging_root, MUSIC_DIR, existing_index=existing_index,
        normalize_key=lambda artist, title: (_normalize_dup_artist(artist), _normalize_dup_title(title)),
        progress_cb=_ipod_job.progress,
    )
    stats["ipod_name"] = ipod_name
    stats["staging_path"] = staging_root
    _ipod_state["result"] = stats


def _start_ipod_import_bg(mount, ipod_name, staging_root, existing_index):
    """Same "already running" convention as the scan/organize jobs above."""
    return _ipod_job.start(
        _run_ipod_import_bg, mount, ipod_name, staging_root, existing_index,
        guard=_library_lock, ipod_name=ipod_name,
    )


def _existing_dup_index():
    """(normalized_artist, normalized_title) -> {"path", "duration"} for the
    real library right now -- same (artist, title) normalization the
    Duplicates feature and the staging/move step already use, so a song
    already in the library under any edition ("Live", "Remastered", a
    different release...) is treated as the same track. Shared by every
    call site that needs to know what's already there before copying iPod
    tracks -- built fresh each time so the skip decision reflects the
    library as it stands right then."""
    db = get_db()
    return {
        (_normalize_dup_artist(r["artist"]), _normalize_dup_title(r["title"])): {"path": r["path"], "duration": r["duration"]}
        for r in db.execute("SELECT artist, title, path, duration FROM tracks").fetchall()
    }


@app.route("/api/ipod/detect")
def ipod_detect():
    # See _run_ipod_import_bg's comment -- no importlib.reload here, it
    # deadlocks this module specifically in the packaged/frozen build.
    import ipod_import
    info = ipod_import.find_ipod()
    pending = ipod_import.list_pending_batches(MUSIC_DIR) if MUSIC_DIR and os.path.isdir(MUSIC_DIR) else []
    resp = {"found": False, "pending": pending}
    if info:
        resp.update(found=True, name=info["name"], track_count=info["track_count"])
    return jsonify(resp)


@app.route("/api/ipod/import", methods=["POST"])
def ipod_import_route():
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        return jsonify({"started": False, "error": "Set a music folder first (the Folder button), then try again."})
    # See _run_ipod_import_bg's comment -- no importlib.reload here, it
    # deadlocks this module specifically in the packaged/frozen build.
    import ipod_import
    # A copy already in flight (e.g. the modal was closed and reopened)
    # re-attaches to it below instead -- checked before the on-disk pending
    # check right after, since a copy that's still running has already
    # written some, but not all, of its files into the staging folder, and
    # that partial folder must never be mistaken for a completed batch
    # that's just waiting for review.
    if _ipod_state["running"]:
        return jsonify({"started": False, "error": "Already running"})
    # A batch from an earlier import that was never reviewed/moved takes
    # priority over starting a new, overlapping one -- the frontend offers
    # to resume that review instead (see runIpodImport() in app.js). One
    # that never finished copying (pending[i]["complete"] is False -- see
    # ipod_import.COMPLETE_MARKER) still takes priority over starting a
    # fresh import over it, but needs a copy to actually finish it, which
    # the review screen's "Continue copying" button drives via
    # /api/ipod/staging/continue-import rather than here.
    pending = ipod_import.list_pending_batches(MUSIC_DIR)
    if pending:
        message = (
            "A previous copy didn't finish -- reconnect the iPod and continue it from the review screen."
            if not pending[0]["complete"] else
            "A previous import is already staged and waiting for review."
        )
        return jsonify({"started": False, "error": message, "pending": pending})
    info = ipod_import.find_ipod()
    if not info:
        return jsonify({"started": False, "error": "No iPod Classic found. Make sure it's connected and shows “Do Not Disconnect.”"})
    staging_root = ipod_import.staging_root_for(MUSIC_DIR, info["name"])
    started = _start_ipod_import_bg(info["mount"], info["name"], staging_root, _existing_dup_index())
    return jsonify({"started": started, "error": None if started else "Already running"})


@app.route("/api/ipod/import-progress")
def ipod_import_progress():
    return jsonify(_ipod_state)


def _resolve_staging_root(ipod_name):
    """Every /api/ipod/staging/* route below acts on "the" currently staged
    batch. A caller normally doesn't need to say which one -- there's only
    ever one in practice -- so this falls back from an explicit ipod_name,
    to whichever import this server process itself just ran, to whatever's
    simply sitting on disk (covers resuming a review after an app restart)."""
    import ipod_import
    name = ipod_name or _ipod_state.get("ipod_name")
    if not name:
        pending = ipod_import.list_pending_batches(MUSIC_DIR) if MUSIC_DIR and os.path.isdir(MUSIC_DIR) else []
        if not pending:
            return None, None
        name = pending[0]["name"]
    return name, ipod_import.staging_root_for(MUSIC_DIR, name)


@app.route("/api/ipod/staging")
def ipod_staging_list():
    """The review screen's data source -- reads tags straight off the
    staged files (no tracks-table rows exist for them yet). `complete`
    tells the review screen whether this batch's copy actually finished
    (ipod_import.COMPLETE_MARKER) or was interrupted partway and still
    needs another copy pass before the batch shown here can be trusted to
    be the whole thing. `expected_total` is a best-effort track count for
    an incomplete batch's "N of M copied" -- only available when the same
    iPod is currently reconnected, since that's the only place the total
    is known once the batch's own copy never got to record it."""
    import ipod_import
    name, staging_root = _resolve_staging_root(request.args.get("ipod_name"))
    if not staging_root or not os.path.isdir(staging_root):
        return jsonify({"ipod_name": name, "staging_path": staging_root, "tracks": [], "complete": True, "expected_total": None})
    complete = ipod_import.is_batch_complete(staging_root)
    expected_total = None
    if not complete:
        info = ipod_import.find_ipod()
        if info and info["name"] == name:
            expected_total = info["track_count"]
    return jsonify({
        "ipod_name": name,
        "staging_path": staging_root,
        "tracks": ipod_import.list_staged_tracks(staging_root),
        "complete": complete,
        "expected_total": expected_total,
    })


@app.route("/api/ipod/staging/continue-import", methods=["POST"])
def ipod_staging_continue_import():
    """Resumes a copy that was interrupted partway (see ipod_staging_list's
    docstring) -- re-runs import_tracks against the exact same staging_root
    the earlier attempt used. Safe/cheap to re-run: import_tracks already
    skips any destination file that's already fully copied (same size as
    its source), so this only copies what's actually still missing. Shares
    _ipod_state/_start_ipod_import_bg with a fresh /api/ipod/import so the
    review screen's "Continue copying" button can reuse the exact same
    /api/ipod/import-progress polling a first-time copy already uses."""
    if not MUSIC_DIR or not os.path.isdir(MUSIC_DIR):
        return jsonify({"started": False, "error": "Set a music folder first (the Folder button), then try again."})
    import ipod_import
    if _ipod_state["running"]:
        return jsonify({"started": False, "error": "Already running"})
    data = request.get_json(force=True, silent=True) or {}
    name, staging_root = _resolve_staging_root(data.get("ipod_name"))
    if not staging_root or not os.path.isdir(staging_root):
        return jsonify({"started": False, "error": "No staged import to continue"})
    if ipod_import.is_batch_complete(staging_root):
        return jsonify({"started": False, "error": "That batch already finished copying."})
    info = ipod_import.find_ipod()
    if not info or info["name"] != name:
        return jsonify({"started": False, "error": f"Reconnect the “{name}” iPod to finish copying the rest of this batch."})
    started = _start_ipod_import_bg(info["mount"], info["name"], staging_root, _existing_dup_index())
    return jsonify({"started": started, "error": None if started else "Already running"})


@app.route("/api/ipod/staging/reveal", methods=["POST"])
def ipod_staging_reveal():
    """Opens the holding folder in Finder -- purely a convenience so you
    can see the actual files sitting there; nothing here reads or writes
    them. macOS only (matches _pick_folder_dialog's own AppleScript-vs-Tk
    split above); a no-op elsewhere since there's no single equivalent
    command worth guessing at."""
    data = request.get_json(force=True, silent=True) or {}
    _name, staging_root = _resolve_staging_root(data.get("ipod_name"))
    if not staging_root or not os.path.isdir(staging_root):
        return jsonify({"ok": False, "error": "No staged import to show"})
    if sys.platform == "darwin":
        subprocess.run(["open", staging_root])
    return jsonify({"ok": True})


# One shared async-job shape for the three in-place staging fixes (names/
# tags/art) -- parameterized on which ipod_import function to run, rather
# than copy-pasting the same thread-launcher three times.
_staging_fix_job = jobs.Job("staging_fix", "Fixing staged tracks", action=None)
_staging_fix_state = _staging_fix_job.state

_STAGING_FIX_FUNCS = {
    "names": "fix_staged_names",
    "tags": "fix_staged_tags",
    "art": "fix_staged_art",
}


def _run_staging_fix_bg(action, staging_root):
    import ipod_import
    func = getattr(ipod_import, _STAGING_FIX_FUNCS[action])
    _staging_fix_state["result"] = func(staging_root, progress_cb=_staging_fix_job.progress)


@app.route("/api/ipod/staging/fix", methods=["POST"])
def ipod_staging_fix():
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action")
    if action not in _STAGING_FIX_FUNCS:
        return jsonify({"started": False, "error": "Unknown fix action"})
    _name, staging_root = _resolve_staging_root(data.get("ipod_name"))
    if not staging_root or not os.path.isdir(staging_root):
        return jsonify({"started": False, "error": "No staged import to fix"})
    started = _staging_fix_job.start(_run_staging_fix_bg, action, staging_root, guard=_library_lock, action=action)
    if not started:
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True, "error": None})


@app.route("/api/ipod/staging/fix-progress")
def ipod_staging_fix_progress():
    return jsonify(_staging_fix_state)


# Moving the reviewed batch into the real library -- chains the same rescan
# + art-backfill the old direct-to-library import used to run right after
# copying, since the moved tracks need real tracks-table rows (and has_art
# flags) exactly the way any other newly-added file does. Duplicate
# checking against the real library happens here, at move time, purely to
# report -- see ipod_import.move_staged_to_library's own docstring for why
# nothing gets held back or skipped over it.
# Not cancellable: a half-moved batch would leave staged and library copies
# both half-present, and this step's chained rescan expects a finished move.
_staging_move_job = jobs.Job("staging_move", "Moving staged tracks into library", cancellable=False)
_staging_move_state = _staging_move_job.state


def _run_staging_move_bg(staging_root):
    progress_cb = _staging_move_job.progress

    try:
        import ipod_import
        with app.app_context():
            db = get_db()
            existing_keys = {
                (_normalize_dup_artist(r["artist"]), _normalize_dup_title(r["title"]))
                for r in db.execute("SELECT artist, title FROM tracks").fetchall()
            }
        result = ipod_import.move_staged_to_library(
            staging_root, MUSIC_DIR, existing_keys=existing_keys,
            normalize_key=lambda artist, title: (_normalize_dup_artist(artist), _normalize_dup_title(title)),
            progress_cb=progress_cb,
        )
        _staging_move_state["result"] = result
    except Exception as e:
        log.exception("Staging move failed")
        _staging_move_state["error"] = str(e)
    finally:
        # Gated on `total`, not the moved count, for the same reason the old
        # direct-import flow gated its own chained rescan this way: even a
        # batch that moves nothing new still needs a rescan the first time,
        # so real files on disk end up with tracks-table rows to show for
        # them. Rescanning an already-indexed folder is cheap either way.
        if not _staging_move_state["error"] and (_staging_move_state["result"] or {}).get("total"):
            # This runs on a bare background thread with no Flask request in
            # flight -- _start_scan_bg's close_db(None) touches flask.g,
            # which raises RuntimeError("Working outside of application
            # context") without one. An explicit app context is enough to
            # satisfy that; there's no real request to tear down here.
            with app.app_context():
                _snapshot_db()
                _start_scan_bg()
        _ipod_state["ipod_name"] = None


@app.route("/api/ipod/staging/move", methods=["POST"])
def ipod_staging_move():
    data = request.get_json(force=True, silent=True) or {}
    _name, staging_root = _resolve_staging_root(data.get("ipod_name"))
    if not staging_root or not os.path.isdir(staging_root):
        return jsonify({"started": False, "error": "No staged import to move"})
    if not _staging_move_job.start(_run_staging_move_bg, staging_root, guard=_library_lock):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True, "error": None})


@app.route("/api/ipod/staging/move-progress")
def ipod_staging_move_progress():
    return jsonify(_staging_move_state)


# Runs right after staging/move above finishes (see runIpodImport() in
# app.js) -- freshly-moved tracks now have real ids, but a plain scan alone
# doesn't populate has_art (that's normally the "deep scan" tag-checker's
# job, see tags_deep_scan/_run_deep_scan_bg above). fix_staged_art already
# covers most of this before the move, but this remains as a safety net for
# whatever the user chose to skip or that the search there missed -- same
# _fetch_and_cache_art mechanism as the per-track "Fetch cover art" button,
# just applied in bulk with the same rate-limiting fill_genres.py already
# uses for the same API.
_ipod_artfill_job = jobs.Job("ipod_artfill", "Fetching cover art for imported tracks")
_ipod_artfill_state = _ipod_artfill_job.state


def _run_ipod_artfill_bg(rel_paths):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = []
        if rel_paths:
            # Not a plain "WHERE path IN (...)" -- rel_paths comes from
            # move_staged_to_library, built from artist/title *tag*
            # text (normally NFC-composed, e.g. accented characters as
            # one codepoint), while the tracks.path this scan just
            # wrote comes from os.walk() on macOS's default filesystem,
            # which hands back names NFD-decomposed instead (the same
            # characters as base letter + separate combining accent).
            # Visually and case-insensitively identical, but a byte-for-
            # byte SQL match on the raw strings misses every accented
            # path -- confirmed against a real import where every
            # accented track's has_art silently stayed unset. Comparing
            # NFC-normalized forms in Python instead of in SQL makes
            # this correct regardless of which form either side is in.
            targets = {unicodedata.normalize("NFC", p) for p in rel_paths}
            rows = [
                r for r in conn.execute("SELECT id, path, artist, title FROM tracks").fetchall()
                if unicodedata.normalize("NFC", r["path"]) in targets
            ]

        needs_art = []
        for row in rows:
            fpath = os.path.join(MUSIC_DIR, row["path"])
            has_artist, has_title, has_art = (
                _read_raw_tag_presence(fpath) if os.path.isfile(fpath) else (True, True, True)
            )
            conn.execute(
                "UPDATE tracks SET has_artist_tag=?, has_title_tag=?, has_art=? WHERE id=?",
                (int(has_artist), int(has_title), int(has_art), row["id"]),
            )
            if not has_art:
                needs_art.append(row)
        conn.commit()

        fetched = 0
        total = len(needs_art)
        _ipod_artfill_job.set(total=total)
        for i, row in enumerate(needs_art):
            try:
                ok, _err = _fetch_and_cache_art(conn, row["id"], row["artist"], row["title"])
                if ok:
                    fetched += 1
            except Exception:
                pass
            _ipod_artfill_job.progress(i + 1, total)
            time.sleep(0.15)  # be polite to Deezer's public API -- see fill_genres.py
        _ipod_artfill_state["result"] = {"checked": len(rows), "needed_art": total, "fetched": fetched}
    finally:
        conn.close()


@app.route("/api/ipod/backfill-art", methods=["POST"])
def ipod_backfill_art():
    data = request.get_json(force=True, silent=True) or {}
    rel_paths = data.get("paths") or []
    if not _ipod_artfill_job.start(_run_ipod_artfill_bg, rel_paths, guard=_library_lock):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True, "error": None})


@app.route("/api/ipod/backfill-art-progress")
def ipod_backfill_art_progress():
    return jsonify(_ipod_artfill_state)


# Same async-job-with-progress-polling shape as the scan above: moving
# thousands of files into per-artist folders is exactly the kind of thing
# that can take minutes and must never block a request while it runs.
# Not cancellable: organize() hands back the whole old->new path map only when
# it finishes, and the library index is repointed from that afterwards --
# stopping partway would leave files moved on disk with the index still
# pointing at their old paths.
_organize_job = jobs.Job("organize", "Organizing by artist", cancellable=False, progress_extra="moved", moved=0)
_organize_state = _organize_job.state


def _run_organize_bg():
    progress_cb = _organize_job.progress

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
        # Recorded after the index points at the new paths, so each move can
        # be matched to its track -- this is what makes "Organize" undoable.
        with journal.operation(DB_PATH, MUSIC_DIR, "organize", "Organized files into artist folders"):
            journal.record_moves(path_moves)

    _organize_state["result"] = stats


def _start_organize_bg():
    return _organize_job.start(_run_organize_bg, guard=_library_lock, prepare=lambda: close_db(None))


@app.route("/api/organize-by-artist", methods=["POST"])
def organize_by_artist_route():
    _snapshot_db()
    started = _start_organize_bg()
    return jsonify({"started": started, "error": None if started else "Already running"})


@app.route("/api/organize-progress")
def organize_progress():
    return jsonify(_organize_state)


_fill_genres_job = jobs.Job("fill_genres", "Filling missing genres", progress_extra="found", found=0)
_fill_genres_state = _fill_genres_job.state


def _run_fill_genres_bg():
    import importlib
    import fill_genres
    importlib.reload(fill_genres)
    with journal.operation(DB_PATH, MUSIC_DIR, "fill_genres", "Filled in missing genres"):
        _fill_genres_state["result"] = fill_genres.fill_missing_genres(progress_cb=_fill_genres_job.progress)


@app.route("/api/fill-genres", methods=["POST"])
def fill_genres_route():
    # 200 either way (not a 409) -- "already running" is an expected,
    # normal outcome for the frontend to branch on, not a request failure,
    # and the shared api() helper throws on any non-2xx response.
    if not _fill_genres_job.start(_run_fill_genres_bg, guard=_library_lock, prepare=lambda: close_db(None)):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True})


@app.route("/api/fill-genres/progress")
def fill_genres_progress():
    return jsonify(_fill_genres_state)


_fill_years_job = jobs.Job("fill_years", "Fixing release years", progress_extra="updated", updated=0)
_fill_years_state = _fill_years_job.state


def _run_fill_years_bg(track_ids):
    import importlib
    import fill_years
    importlib.reload(fill_years)
    _snapshot_db()
    with journal.operation(DB_PATH, MUSIC_DIR, "fill_years", "Corrected release years"):
        _fill_years_state["result"] = fill_years.fix_release_years(progress_cb=_fill_years_job.progress, track_ids=track_ids)


@app.route("/api/fill-years", methods=["POST"])
def fill_years_route():
    """Corrects tracks toward their original release year using Deezer.
    Pass {"track_ids": [...]} to scope it (e.g. to the current filtered
    view); omit it to run across the whole library."""
    data = request.get_json(force=True, silent=True) or {}
    track_ids = data.get("track_ids") or None
    if not _fill_years_job.start(_run_fill_years_bg, track_ids, guard=_library_lock, prepare=lambda: close_db(None)):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True})


@app.route("/api/fill-years/progress")
def fill_years_progress():
    return jsonify(_fill_years_state)


_fill_art_job = jobs.Job(
    "fill_art", "Finding cover art", progress_extra="fixed", fixed=0,
    summarize=lambda s: "found art for {f} of {c} tracks".format(
        f=(s["result"] or {}).get("fixed", 0), c=(s["result"] or {}).get("checked", 0)),
)
_fill_art_state = _fill_art_job.state


def _run_fill_art_bg(track_ids):
    progress_cb = _fill_art_job.progress

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if track_ids:
            placeholders = ",".join("?" * len(track_ids))
            rows = conn.execute(
                f"SELECT id, artist, title FROM tracks WHERE has_art=0 AND id IN ({placeholders})", track_ids,
            ).fetchall()
        else:
            rows = conn.execute("SELECT id, artist, title FROM tracks WHERE has_art=0").fetchall()
        total = len(rows)
        fixed = 0
        for i, row in enumerate(rows):
            try:
                ok, _err = _fetch_and_cache_art(conn, row["id"], row["artist"], row["title"])
                if ok:
                    fixed += 1
            except Exception:
                pass
            progress_cb(i + 1, total, fixed)
            time.sleep(0.15)  # be polite to the free APIs in art_lookup.py
        _fill_art_state["result"] = {"checked": total, "fixed": fixed}
    finally:
        conn.close()


@app.route("/api/fill-art", methods=["POST"])
def fill_art_route():
    """Best-effort cover art for every track the deep scan found missing
    it (has_art=0), tried across several free sources -- see
    art_lookup.py. Pass {"track_ids": [...]} to scope it (e.g. to exactly
    the deep scan's own "Missing cover art" list); omit it to run across
    the whole library."""
    data = request.get_json(force=True, silent=True) or {}
    track_ids = data.get("track_ids") or None
    if not _fill_art_job.start(_run_fill_art_bg, track_ids, guard=_library_lock, prepare=lambda: close_db(None)):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True})


@app.route("/api/fill-art/progress")
def fill_art_progress():
    return jsonify(_fill_art_state)


_unify_genre_job = jobs.Job("unify_genre", "Unifying genre per artist", progress_extra="updated", updated=0)
_unify_genre_state = _unify_genre_job.state


def _run_unify_genre_bg():
    import importlib
    import unify_artist_genre
    importlib.reload(unify_artist_genre)
    with journal.operation(DB_PATH, MUSIC_DIR, "unify_genre", "Unified genre per artist"):
        _unify_genre_state["result"] = unify_artist_genre.unify_artist_genres(progress_cb=_unify_genre_job.progress)


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
    if not _unify_genre_job.start(
        _run_unify_genre_bg, guard=_library_lock,
        prepare=lambda: (_snapshot_db(), close_db(None)),
    ):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True})


@app.route("/api/unify-artist-genre/progress")
def unify_artist_genre_progress():
    return jsonify(_unify_genre_state)


_fix_artist_title_job = jobs.Job("fix_artist_title", "Correcting artist & track names", progress_extra="updated", updated=0)
_fix_artist_title_state = _fix_artist_title_job.state


def _run_fix_artist_title_bg():
    import importlib
    import fix_artist_title
    importlib.reload(fix_artist_title)
    with journal.operation(DB_PATH, MUSIC_DIR, "fix_artist_title", "Corrected artist & track names"):
        _fix_artist_title_state["result"] = fix_artist_title.fix_artist_title(progress_cb=_fix_artist_title_job.progress)


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
    if not _fix_artist_title_job.start(
        _run_fix_artist_title_bg, guard=_library_lock,
        prepare=lambda: (_snapshot_db(), close_db(None)),
    ):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True})


@app.route("/api/fix-artist-title/progress")
def fix_artist_title_progress():
    return jsonify(_fix_artist_title_state)


# Bulk audio-fingerprint verification -- unlike every other bulk tool
# above, this never writes anything on its own: a fingerprint match is
# still just one more opinion (a mislabeled file, a cover version, an
# ambiguous short clip can all produce a plausible-looking "mismatch"),
# so it only reports candidates for the user to review and apply -- the
# same review-before-acting shape as the Duplicates panel's own
# groups_skipped list, and it reuses /api/tags/<id> (already writes both
# file and index) for the actual fix once someone accepts one.
_verify_audio_job = jobs.Job("verify_audio", "Verifying tags against audio")
_verify_audio_state = _verify_audio_job.state


def _run_verify_audio_bg(track_ids):
    progress_cb = _verify_audio_job.progress

    fpcalc_path = _find_binary("fpcalc")
    if not fpcalc_path:
        raise RuntimeError("fpcalc_missing")
    api_key = (jukebox_config.load_config().get("acoustidApiKey") or "").strip()
    if not api_key:
        raise RuntimeError("no_api_key")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if track_ids:
            placeholders = ",".join("?" * len(track_ids))
            rows = conn.execute(
                f"SELECT id, path, artist, title FROM tracks WHERE id IN ({placeholders})", track_ids,
            ).fetchall()
        else:
            rows = conn.execute("SELECT id, path, artist, title FROM tracks").fetchall()
    finally:
        conn.close()

    total = len(rows)
    mismatches = []
    checked = 0
    errors = 0
    for i, row in enumerate(rows):
        fpath = os.path.join(MUSIC_DIR, row["path"])
        if os.path.isfile(fpath):
            try:
                found_artist, found_title, score = _fingerprint_lookup(fpath, api_key, fpcalc_path)
                checked += 1
                if found_title is not None and _tags_look_mismatched(row["artist"], row["title"], found_artist, found_title):
                    mismatches.append({
                        "id": row["id"], "score": score,
                        "current_artist": row["artist"], "current_title": row["title"],
                        "found_artist": found_artist, "found_title": found_title,
                    })
            except Exception:
                errors += 1
        progress_cb(i + 1, total)
        time.sleep(0.35)  # AcoustID asks for at most ~3 requests/second per API key
    _verify_audio_state["result"] = {"checked": checked, "errors": errors, "mismatches": mismatches}


@app.route("/api/verify-audio", methods=["POST"])
def verify_audio_route():
    """Pass {"track_ids": [...]} to scope it (the selection toolbar always
    does -- fingerprinting is real per-track work, both decoding audio and
    an AcoustID round trip, so this is opt-in on a selection rather than
    silently defaulting to the whole library); omit it to run across
    everything, accepting that cost."""
    data = request.get_json(force=True, silent=True) or {}
    track_ids = data.get("track_ids") or None
    if not _verify_audio_job.start(_run_verify_audio_bg, track_ids, guard=_library_lock, prepare=lambda: close_db(None)):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True})


@app.route("/api/verify-audio/progress")
def verify_audio_progress():
    return jsonify(_verify_audio_state)


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


def _tags_look_mismatched(current_artist, current_title, found_artist, found_title):
    """Loose text comparison between a track's current tags and what audio
    fingerprinting found -- deliberately lenient (artist-credit formatting/
    ordering varies between MusicBrainz and however a file happens to be
    tagged, e.g. "Bill Medley & Jennifer Warnes" vs "Bill Medley, Jennifer
    Warnes"), so this only flags a likely-real mismatch (the fingerprint
    found a genuinely different song) rather than every stylistic
    difference. Substring-based, same convention art_lookup.py/fill_genres.py
    already use for their own same-artist filtering. Strips to word chars
    (Unicode letters/digits, not just a-z0-9) -- an a-z0-9-only strip reduces a
    fully non-Latin title (Japanese, Cyrillic, ...) to an empty string on
    both sides, which the emptiness checks below then always treat as an
    automatic, unverified match."""
    def norm(s):
        return re.sub(r"[^\w]", "", (s or "").lower())
    cur_artist, cur_title = norm(current_artist), norm(current_title)
    found_artist_n, found_title_n = norm(found_artist), norm(found_title)
    artist_ok = not cur_artist or not found_artist_n or cur_artist in found_artist_n or found_artist_n in cur_artist
    title_ok = not cur_title or not found_title_n or cur_title in found_title_n or found_title_n in cur_title
    return not (artist_ok and title_ok)


@app.route("/api/tracks/<int:track_id>/verify-audio", methods=["POST"])
def verify_track_audio(track_id):
    """Audio fingerprint check (Chromaprint/AcoustID, same tools/account as
    the Live Radio song-ID feature) for one track: does the file's actual
    audio content match its current artist/title tags? Catches what the
    Deezer-backed "Fix artist & track names" tool structurally can't --
    that tool only confirms the current tags name a real, existing song,
    which is equally true whether or not this particular audio file is
    that song. A file mislabeled with a different, genuinely real song's
    tags passes that check by design; this checks the audio itself
    instead. Synchronous -- one fingerprint + one lookup for a single
    track, same cost class as lookup_track_tags above."""
    fpcalc_path = _find_binary("fpcalc")
    if not fpcalc_path:
        return jsonify({"ok": False, "error": "fpcalc_missing"})
    api_key = (jukebox_config.load_config().get("acoustidApiKey") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "no_api_key"})

    db = get_db()
    row = db.execute("SELECT path, artist, title FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)
    fpath = os.path.join(MUSIC_DIR, row["path"])
    if not os.path.isfile(fpath):
        return jsonify({"ok": False, "error": "File not found on disk"})

    try:
        found_artist, found_title, score = _fingerprint_lookup(fpath, api_key, fpcalc_path)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    if found_title is None:
        return jsonify({"ok": False, "error": "no_match"})

    mismatched = _tags_look_mismatched(row["artist"], row["title"], found_artist, found_title)
    return jsonify({
        "ok": True, "mismatched": mismatched, "score": score,
        "current_artist": row["artist"], "current_title": row["title"],
        "found_artist": found_artist, "found_title": found_title,
    })


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
    file's own tags (see tagio.py -- shared with the bulk tools, and what
    journals each change so it can be undone)."""
    return tagio.write_tag(fpath, field, value)


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

    with journal.operation(DB_PATH, MUSIC_DIR, "edit_tags", "Edited tags by hand", batch=data.get("batch")):
        wrote = _write_file_tag(fpath, field, value)
    if not wrote:
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
        # Only reachable for "album" -- the whitelist check at the top of
        # this function is what makes interpolating `field` here safe;
        # don't widen that whitelist without checking this stays true.
        db.execute(f"UPDATE tracks SET {field}=? WHERE id=?", (value, track_id))
    db.commit()
    if field in ("artist", "title"):
        # These are exactly what duplicate-grouping keys off of.
        _invalidate_dup_plan_cache()
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


_deep_scan_job = jobs.Job("deep_scan", "Deep tag scan")
_deep_scan_state = _deep_scan_job.state
_DEEP_SCAN_WORKERS = 8  # I/O-bound (opening+reading file headers), not CPU-bound -- threads are fine


def _run_deep_scan_bg(rows):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total = len(rows)
        checked = 0

        def _check_one(row):
            fpath = os.path.join(MUSIC_DIR, row["path"])
            presence = (
                _read_raw_tag_presence(fpath) if os.path.isfile(fpath) else (True, True, True)
            )
            return row["id"], presence

        # Reading each file (open + parse tag header) is the slow part
        # and fully independent per file -- farming it out across a
        # small thread pool overlaps that I/O instead of doing it one
        # file at a time, while the actual sqlite writes stay on this
        # one thread/connection either way (sqlite doesn't want
        # concurrent writers, and they're cheap compared to the reads).
        pool = ThreadPoolExecutor(max_workers=_DEEP_SCAN_WORKERS)
        try:
            for track_id, (has_artist, has_title, has_art) in pool.map(_check_one, rows):
                conn.execute(
                    "UPDATE tracks SET has_artist_tag=?, has_title_tag=?, has_art=? WHERE id=?",
                    (int(has_artist), int(has_title), int(has_art), track_id),
                )
                checked += 1
                _deep_scan_job.progress(checked, total)
                if checked % 500 == 0:
                    conn.commit()
        except BaseException:
            # Cancelled or failed: don't make `with`-style shutdown sit
            # through every still-queued file read first.
            pool.shutdown(wait=False, cancel_futures=True)
            conn.commit()
            raise
        pool.shutdown()
        conn.commit()

        result = {"checked": checked}
        for key, col in (("artist", "has_artist_tag"), ("title", "has_title_tag"), ("art", "has_art")):
            found = conn.execute(
                f"SELECT id, artist, title, album, year, primary_genre as genre FROM tracks "
                f"WHERE {col} = 0 ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE"
            ).fetchall()
            tracks = [dict(r) for r in found]
            result[key] = {"count": len(tracks), "tracks": tracks}
        _deep_scan_state["result"] = result
    finally:
        conn.close()


@app.route("/api/tags/deep-scan", methods=["POST"])
def tags_deep_scan():
    """Opens every file directly to check for a real artist tag, title tag,
    and embedded cover art -- slow (a full pass over the library), unlike
    the instant DB-only audit above. Runs in the background with progress
    polling (same shape as fill-genres/fill-years/rescan) since a blocking
    request with no feedback over tens of thousands of files looks
    indistinguishable from hung. Results are cached on the tracks table so
    this only needs to re-run when the user explicitly asks for it."""
    db = get_db()
    rows = [dict(r) for r in db.execute("SELECT id, path FROM tracks").fetchall()]

    if not _deep_scan_job.start(_run_deep_scan_bg, rows, guard=_library_lock, prepare=lambda: close_db(None), total=len(rows)):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True, "total": len(rows)})


@app.route("/api/tags/deep-scan/progress")
def tags_deep_scan_progress():
    return jsonify(_deep_scan_state)


@app.route("/api/config")
def get_config():
    return jsonify({"music_dir": MUSIC_DIR, "music_dir_exists": bool(MUSIC_DIR and os.path.isdir(MUSIC_DIR))})


@app.route("/api/theme")
def get_theme():
    return jsonify({"theme": jukebox_config.load_config().get("theme")})


# Kept in sync with static/index.html's <option> lists by hand -- these
# only change when a new theme/finish/design/color is added to the UI,
# which already means editing index.html anyway. Rejecting anything else
# means a bad value (a stray API call, a typo in a future frontend change)
# can't get saved and silently produce an unstyled/broken UI with no
# fallback and no error, which is what plain isinstance(str)-only
# validation allowed before.
VALID_THEMES = {"default", "graphite", "hifi", "cassette", "vinyl"}
VALID_WOOD_FINISHES = {"walnut", "ebony", "mahogany"}
VALID_CASSETTE_DESIGNS = {"blue", "red", "rust"}
VALID_VU_COLORS = {"amber", "blue", "green"}
# Toolbar/sidebar/track-list arrangement -- independent of the player theme
# above (which only ever restyles the now-playing stage). "classic" is
# today's fixed layout; the other three are opt-in decluttering options a
# user picks from the same place as the player theme.
VALID_LAYOUTS = {"classic", "command-bar", "grouped-ribbon", "row-density"}


@app.route("/api/layout-mode")
def get_layout_mode():
    return jsonify({"layoutMode": jukebox_config.load_config().get("layoutMode")})


@app.route("/api/layout-mode", methods=["POST"])
def set_layout_mode():
    data = request.get_json(force=True, silent=True) or {}
    layout_mode = data.get("layoutMode")
    if layout_mode not in VALID_LAYOUTS:
        abort(400)
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("layoutMode", layout_mode))
    return jsonify({"ok": True})


@app.route("/api/theme", methods=["POST"])
def set_theme():
    """Saved server-side (not just localStorage) so the last theme used
    carries over in the pywebview desktop app too -- its WKWebView doesn't
    persist localStorage across separate app launches the way a real
    browser tab does, so localStorage alone silently resets there."""
    data = request.get_json(force=True, silent=True) or {}
    theme = data.get("theme")
    if theme not in VALID_THEMES:
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
    if wood_finish not in VALID_WOOD_FINISHES:
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
    if cassette_design not in VALID_CASSETTE_DESIGNS:
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
    if vu_color not in VALID_VU_COLORS:
        abort(400)
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("vuColor", vu_color))
    return jsonify({"ok": True})


def _pick_folder_dialog(prompt="Select the music folder for Notorious B.P.M. to scan"):
    """Native folder picker, per OS: AppleScript on macOS (no extra deps),
    Tk's file dialog everywhere else (bundled with the standard library, so
    it's available even in a PyInstaller-frozen build with no other GUI
    toolkit around). Returns the picked path, or None if cancelled."""
    if sys.platform == "darwin":
        escaped_prompt = prompt.replace('"', '\\"')
        result = subprocess.run(
            ["osascript", "-e",
             f'POSIX path of (choose folder with prompt "{escaped_prompt}")'],
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
        picked = filedialog.askdirectory(title=prompt)
    finally:
        root.destroy()
    return picked or None


def _pick_save_file_dialog(prompt, default_name):
    """Native save-file picker, per OS -- same AppleScript-vs-Tk split as
    _pick_folder_dialog just above, for "New Library" choosing where to
    save the library file itself. (The existing "Backup" export button
    reaches a similar native dialog through the pywebview JS-API bridge in
    desktop_macos.py/launcher.py instead -- that's a difference in how that
    feature happened to get built, not a technical requirement; a plain
    Flask route works exactly the same way _pick_folder_dialog's own
    osascript call already does.) Returns the picked path, or None if
    cancelled."""
    if sys.platform == "darwin":
        escaped_prompt = prompt.replace('"', '\\"')
        escaped_name = default_name.replace('"', '\\"')
        result = subprocess.run(
            ["osascript", "-e",
             f'POSIX path of (choose file name with prompt "{escaped_prompt}" default name "{escaped_name}")'],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        picked = filedialog.asksaveasfilename(
            title=prompt, initialfile=default_name,
            defaultextension=library_manager.LIBRARY_EXT,
            filetypes=[("Notorious B.P.M. Library", f"*{library_manager.LIBRARY_EXT}")],
        )
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
    # Into the *current* library's own metadata, not the flat config key --
    # that key stops meaning anything once multiple libraries exist (kept
    # only for the one-time legacy-migration check in library_manager.py).
    library_manager.write_music_dir(DB_PATH, picked)
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


# ------------------------------------------------------------------ library --
# Multiple, independent, switchable libraries -- each one a single .nbpmlib
# SQLite file the user names and saves wherever they like (an external
# drive, anywhere), holding just the catalog (tags, playlists, ratings) and
# its own music_dir pointer; music files themselves stay wherever they are.
# See library_manager.py for the config/metadata side of this; everything
# here is the runtime side -- actually swapping which DB/folder the rest of
# this file's ~44 routes are reading and writing.

def _switch_library(db_path, music_dir):
    """Switches every route in this file over to a different library.
    Refuses while any background job is running, and holds _library_lock
    for its own whole duration so a job can't sneak in and start mid-
    switch either -- see _library_lock's own comment near the top of this
    file for why both directions matter."""
    global DB_PATH, MUSIC_DIR, ART_CACHE_DIR, TRASH_DIR, BACKUP_DIR, _schema_ready
    with _library_lock:
        if _any_background_job_running():
            return {"ok": False, "error": "A background job is running — wait for it to finish, then try again."}

        _snapshot_db()  # snapshots the library being LEFT, via its own (still current) BACKUP_DIR
        close_db(None)

        comp = library_manager.companion_dir(db_path)
        new_art_cache = os.path.join(comp, "art_cache")
        new_trash = os.path.join(comp, "trash")
        new_backup = os.path.join(comp, "backups")
        os.makedirs(new_art_cache, exist_ok=True)
        os.makedirs(new_trash, exist_ok=True)
        os.makedirs(new_backup, exist_ok=True)

        DB_PATH = db_path
        MUSIC_DIR = music_dir
        ART_CACHE_DIR = new_art_cache
        TRASH_DIR = new_trash
        BACKUP_DIR = new_backup
        # Without this, get_db() never runs _ensure_deep_scan_columns()
        # again (it's gated to run once per process) -- a freshly-opened
        # library would silently be missing columns/tables on first use.
        _schema_ready = False

        os.environ["JUKEBOX_DB_PATH"] = db_path
        if music_dir:
            os.environ["JUKEBOX_MUSIC_DIR"] = music_dir
        else:
            os.environ.pop("JUKEBOX_MUSIC_DIR", None)

        return {"ok": True}


@app.route("/api/library/current")
def library_current():
    return jsonify(library_manager.get_current() or {"path": None, "name": None, "music_dir": None})


@app.route("/api/library/recent")
def library_recent():
    return jsonify(library_manager.list_recent())


@app.route("/api/library/new", methods=["POST"])
def library_new():
    """Chains both native dialogs server-side (save location + name, then
    the music folder) so the frontend just makes one round-trip -- same
    "why a plain Flask route is fine here" reasoning as _pick_save_file_dialog
    itself. Creates the library, switches to it, and kicks off its first
    scan the same way /api/choose-folder already does."""
    if _any_background_job_running():
        return jsonify({"ok": False, "error": "A background job is running — wait for it to finish, then try again."})
    save_path = _pick_save_file_dialog(
        "Save your new library as:", f"New Library{library_manager.LIBRARY_EXT}",
    )
    if not save_path:
        return jsonify({"ok": False, "cancelled": True})
    music_dir = _pick_folder_dialog("Select the music folder for this library")
    if not music_dir or not os.path.isdir(music_dir):
        return jsonify({"ok": False, "cancelled": True})

    db_path = library_manager.create_new(save_path, music_dir)
    result = _switch_library(db_path, music_dir)
    if not result["ok"]:
        return jsonify(result)
    started = _start_scan_bg()
    return jsonify({
        "ok": True, "path": db_path, "name": library_manager.display_name(db_path),
        "music_dir": music_dir, "started": started,
    })


@app.route("/api/library/open", methods=["POST"])
def library_open():
    """Body may include a `path` (picking a specific entry from the Recent
    list); with no path, runs a native file-choose dialog instead."""
    if _any_background_job_running():
        return jsonify({"ok": False, "error": "A background job is running — wait for it to finish, then try again."})
    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path")
    if not path:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["osascript", "-e",
                 f'POSIX path of (choose file with prompt "Open a Notorious B.P.M. library" '
                 f'of type {{"nbpmlib"}})'],
                capture_output=True, text=True,
            )
            path = result.stdout.strip() or None if result.returncode == 0 else None
        else:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                path = filedialog.askopenfilename(
                    title="Open a Notorious B.P.M. library",
                    filetypes=[("Notorious B.P.M. Library", f"*{library_manager.LIBRARY_EXT}")],
                ) or None
            finally:
                root.destroy()
        if not path:
            return jsonify({"ok": False, "cancelled": True})

    opened = library_manager.open_existing(path)
    if not opened["ok"]:
        return jsonify(opened)
    result = _switch_library(opened["path"], opened["music_dir"])
    if not result["ok"]:
        return jsonify(result)
    return jsonify({
        "ok": True, "path": opened["path"], "name": library_manager.display_name(opened["path"]),
        "music_dir": opened["music_dir"],
    })


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


def _convert_output_dir():
    """Where converted files get written -- whatever the user last picked
    via /api/convert/output-dir, or convert_audio's own default the first
    time (a real folder under ~/Music, never silently chosen without the
    user being told where it is -- see that route for the actual picker)."""
    import convert_audio
    return jukebox_config.load_config().get("convertOutputDir") or convert_audio.OUTPUT_ROOT


@app.route("/api/convert/output-dir")
def get_convert_output_dir():
    return jsonify({"path": _convert_output_dir()})


@app.route("/api/convert/output-dir", methods=["POST"])
def set_convert_output_dir():
    picked = _pick_folder_dialog("Select where Notorious B.P.M. should save converted files")
    if not picked:
        return jsonify({"ok": False, "cancelled": True})
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("convertOutputDir", picked))
    return jsonify({"ok": True, "path": picked})


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
        out_path, already_existed = convert_audio.convert(fpath, row["artist"], row["title"], fmt, _convert_output_dir())
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


_convert_job = jobs.Job("convert", "Converting audio", results=None)
_convert_state = _convert_job.state
_CONVERT_WORKERS = 4  # each is a real ffmpeg transcode -- CPU/disk heavy, unlike the tag-scan's light file reads


def _convert_one_file(track_id, fpath, artist, title, fmt, output_root):
    """The actual conversion work, run in a worker thread -- deliberately
    takes plain file/tag values rather than a db handle, so no sqlite
    connection is ever touched off the main thread (sqlite3 connections
    aren't safe to share across concurrent threads)."""
    import convert_audio
    try:
        out_path, already_existed = convert_audio.convert(fpath, artist, title, fmt, output_root)
        return {"track_id": track_id, "ok": True, "output_path": out_path, "already_existed": already_existed}
    except Exception as e:
        return {"track_id": track_id, "ok": False, "error": str(e)}


def _run_convert_bg(track_ids, fmt):
    import importlib
    import convert_audio
    importlib.reload(convert_audio)
    output_root = _convert_output_dir()

    # All the sqlite reads happen here, up front, on this one thread --
    # the thread pool below only ever calls _convert_one_file, which
    # touches files and ffmpeg, never the database.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(track_ids))
        rows_by_id = {
            r["id"]: r for r in conn.execute(
                f"SELECT id, path, artist, title, ext FROM tracks WHERE id IN ({placeholders})",
                track_ids,
            ).fetchall()
        }
    finally:
        conn.close()

    total = len(track_ids)
    done = 0
    results = [None] * total
    to_submit = {}  # future -> index, only for tracks that actually need converting
    for i, track_id in enumerate(track_ids):
        row = rows_by_id.get(track_id)
        if not row:
            results[i] = {"track_id": track_id, "ok": False, "error": "track not found"}
            continue
        src_ext = row["ext"].lstrip(".").lower()
        src_fmt = {"flac": "flac", "m4a": "alac", "mp3": "mp3320"}.get(src_ext)
        if src_fmt == fmt:
            results[i] = {
                "track_id": track_id, "ok": False,
                "error": f"already {convert_audio.FORMATS[fmt]['label']}",
            }
            continue
        fpath = os.path.join(MUSIC_DIR, row["path"])
        to_submit[i] = (track_id, fpath, row["artist"], row["title"])

    done = total - len(to_submit)
    _convert_job.progress(done, total)

    pool = ThreadPoolExecutor(max_workers=_CONVERT_WORKERS)
    try:
        futures = {
            pool.submit(_convert_one_file, track_id, fpath, artist, title, fmt, output_root): i
            for i, (track_id, fpath, artist, title) in to_submit.items()
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
            done += 1
            _convert_job.progress(done, total)
    except BaseException:
        # Cancelled: queued conversions never start; any ffmpeg already
        # running finishes its own file (killing it mid-write would
        # leave a truncated output behind).
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()

    ok = sum(1 for r in results if r["ok"])
    _convert_state["results"] = {
        "total": total, "converted": ok, "failed": total - ok, "results": results,
    }


def _start_convert(track_ids, fmt):
    started = _convert_job.start(
        _run_convert_bg, track_ids, fmt, guard=_library_lock,
        prepare=lambda: close_db(None), total=len(track_ids),
    )
    return len(track_ids) if started else None


@app.route("/api/convert-tracks", methods=["POST"])
def convert_tracks():
    """Bulk-converts a set of tracks in the background (each one is a real
    ffmpeg transcode -- for a big selection, minutes rather than seconds),
    with progress polling, same shape as the other long-running jobs."""
    data = request.get_json(force=True, silent=True) or {}
    fmt = data.get("format")
    track_ids = data.get("track_ids") or []
    import importlib
    import convert_audio
    importlib.reload(convert_audio)
    if fmt not in convert_audio.FORMATS or not track_ids:
        abort(400)
    total = _start_convert(track_ids, fmt)
    if total is None:
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True, "total": total})


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
    if not track_ids:
        abort(400)
    total = _start_convert(track_ids, fmt)
    if total is None:
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True, "total": total})


@app.route("/api/convert-tracks/progress")
def convert_tracks_progress():
    return jsonify(_convert_state)


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

    limit = min(_parse_int_arg(request.args.get("limit", 30), "limit"), 100)
    offset = _parse_int_arg(request.args.get("offset", 0), "offset")
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


_SAME_QUALITY_TOLERANCE = 0.05  # apparent bitrate within 5% counts as "no real difference"


def _resolve_same_recording(tracks):
    """For a group where every copy is a "plain" version (no Live/Remaster/
    Mix marker to prefer one over another -- see _plan_auto_clean's
    "no_edition_to_remove" case), tries to pick a keeper anyway: same
    artist and title already established the group, so if the durations
    also agree closely, these are almost certainly the same recording
    from different sources/rips rather than different songs that happen
    to share a title.

    Normally prefers the highest apparent bitrate (file size ÷ duration --
    cheap to compute, no need to actually decode anything). But when every
    copy shares the same format and that bitrate is within a few percent
    across all of them -- i.e. there's no real quality difference to
    prefer by, just multiple copies of what's almost certainly the exact
    same rip -- picks the OLDEST file (by mtime) instead: whichever copy
    showed up more recently is the more likely redundant re-download, not
    the other way around. mtime rather than a true creation time since
    it's the one timestamp that means the same thing and is reliably
    available on every OS this app runs on; a downloaded audio file is
    essentially never modified after the fact, so "last written" and
    "created" land on the same moment in practice.

    Returns None (leave for manual review) rather than guess when:
    - there are fewer than 2 usable tracks (nothing to compare)
    - any duration is missing (can't judge "close enough")
    - durations disagree by more than a small tolerance -- a real
      difference here (not just encoder rounding) more likely means a
      genuinely different edit/recording despite the identical title,
      which this should never silently delete one side of
    - any file is missing or unreadable (can't score it -- safer to
      leave the whole group for a human than guess blind)
    """
    if len(tracks) < 2:
        return None
    durations = [t.get("duration") for t in tracks]
    if any(d is None for d in durations):
        return None
    tolerance = max(2.0, 0.03 * (sum(durations) / len(durations)))
    if max(durations) - min(durations) > tolerance:
        return None

    scored = []
    for t in tracks:
        fpath = os.path.join(MUSIC_DIR, t["path"])
        try:
            stat = os.stat(fpath)
        except OSError:
            return None
        duration = t["duration"] or 0
        bitrate_proxy = (stat.st_size / duration) if duration else 0
        scored.append({"track": t, "bitrate_proxy": bitrate_proxy, "ext": t.get("ext"), "mtime": stat.st_mtime})

    scored.sort(key=lambda s: s["bitrate_proxy"], reverse=True)
    best = scored[0]
    same_quality = all(
        s["ext"] == best["ext"]
        and (best["bitrate_proxy"] == 0 or abs(s["bitrate_proxy"] - best["bitrate_proxy"]) / best["bitrate_proxy"] <= _SAME_QUALITY_TOLERANCE)
        for s in scored
    )
    if same_quality:
        scored.sort(key=lambda s: s["mtime"])  # oldest first

    keep = scored[0]["track"]
    remove = [s["track"] for s in scored[1:]]
    return keep, remove


def _plan_auto_clean(db):
    """For each duplicate group that has both a plain (album) version and at
    least one Live/Remastered/Mix version, delete only the Live/Remastered/
    Mix ones -- every plain version is left untouched, so this can never
    remove someone's only copy of a studio track. Groups with no plain
    version (nothing to prefer) are skipped outright (all_non_album).
    Groups where every copy is plain (no_edition_to_remove) get a second
    chance below via _resolve_same_recording before giving up on them.
    Separately, repeat-downloaded copies (see _find_repeat_download_dupes)
    are folded into the same removal list, since those are just as safe to
    clean up automatically and are often the bulk of a library's actual
    duplicates -- titles/albums that are identical rather than an edition
    variant, which the title-based grouping above never flags at all."""
    groups = _find_duplicate_groups(db)
    to_delete = []
    groups_cleaned = 0
    same_recording_cleaned = 0
    skipped = []  # (group, reason) -- filtered against repeat-download results below
    for group in groups:
        plain = [t for t in group["tracks"] if not _is_non_album_version(t)]
        non_album = [t for t in group["tracks"] if _is_non_album_version(t)]
        if not plain:
            skipped.append((group, "all_non_album"))
            continue
        if not non_album:
            # Every copy is "plain" -- no edition marker to prefer one
            # over another by title alone. Worth a second look: if their
            # durations agree closely too, they're almost certainly the
            # same recording from different sources, safe to resolve by
            # apparent audio quality instead of leaving for manual review.
            resolved = _resolve_same_recording(plain)
            if resolved is None:
                skipped.append((group, "no_edition_to_remove"))
                continue
            _keep, remove = resolved
            same_recording_cleaned += 1
            to_delete.extend(remove)
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

    return to_delete, groups_cleaned, skipped_groups, len(repeat_groups), same_recording_cleaned


# _plan_auto_clean() does two full-table scans (title/artist regex
# normalization over every track, plus a second pass clustering by
# filename) -- cheap for one call, but the duplicates panel's own normal
# use recomputes it from scratch on every single call: opening the panel,
# every page of the review list, each one a fresh multi-second pass over
# the whole library for data that hasn't changed since the panel opened.
# Cached here, invalidated explicitly wherever tracks actually change
# (scan, tag edits to artist/title, any duplicate-related delete) with a
# short TTL as a safety net for any mutation path that doesn't explicitly
# invalidate it.
_dup_plan_cache = {"result": None, "computed_at": 0.0}
_DUP_PLAN_CACHE_TTL = 30  # seconds
_dup_plan_cache_lock = threading.Lock()


def _get_cached_dup_plan(db):
    with _dup_plan_cache_lock:
        cached = _dup_plan_cache["result"]
        if cached is not None and (time.time() - _dup_plan_cache["computed_at"]) < _DUP_PLAN_CACHE_TTL:
            return cached
    result = _plan_auto_clean(db)
    with _dup_plan_cache_lock:
        _dup_plan_cache["result"] = result
        _dup_plan_cache["computed_at"] = time.time()
    return result


def _invalidate_dup_plan_cache():
    with _dup_plan_cache_lock:
        _dup_plan_cache["result"] = None


_dup_clean_job = jobs.Job("dup_clean", "Removing duplicates", deleted=0, errors=None)
_dup_clean_state = _dup_clean_job.state


def _run_dup_clean_bg(rows):
    progress_cb = _dup_clean_job.progress

    try:
        # A fresh connection, not get_db()'s Flask-request-scoped one --
        # this runs on a background thread with no request context.
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")  # so deleted tracks take their ratings/playlist entries with them
        try:
            deleted, errors = _delete_track_rows(conn, rows, progress_cb=progress_cb)
            _dup_clean_state["deleted"] = deleted
            _dup_clean_state["errors"] = errors
        finally:
            conn.close()
    finally:
        _invalidate_dup_plan_cache()


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
    to_delete, groups_cleaned, skipped_groups, repeat_groups_cleaned, same_recording_cleaned = _get_cached_dup_plan(db)
    groups_skipped = len(skipped_groups)

    if dry_run:
        return jsonify({
            "tracks_to_delete": len(to_delete),
            "groups_cleaned": groups_cleaned,
            "groups_skipped": groups_skipped,
            "repeat_groups_cleaned": repeat_groups_cleaned,
            "same_recording_cleaned": same_recording_cleaned,
            "tracks": [{"id": t["id"], "artist": t["artist"], "title": t["title"]} for t in to_delete],
            "skipped_groups": skipped_groups,
        })

    if not _dup_clean_job.start(_run_dup_clean_bg, to_delete, guard=_library_lock, prepare=lambda: close_db(None), total=len(to_delete)):
        return jsonify({"started": False, "error": "Already running"})
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
    _to_delete, _groups_cleaned, skipped_groups, _repeat_groups_cleaned, _same_recording_cleaned = _get_cached_dup_plan(db)

    limit = min(_parse_int_arg(request.args.get("limit", 20), "limit"), 50)
    offset = _parse_int_arg(request.args.get("offset", 0), "offset")
    page = skipped_groups[offset:offset + limit]

    return jsonify({
        "groups": [
            {"artist": g["artist"], "reason": g["reason"], "tracks": g["tracks"]}
            for g in page
        ],
        "total_groups": len(skipped_groups),
    })


def _parse_int_arg(value, field_name):
    """Query params that get fed straight into int() (limit, offset, decade,
    rating, ...) used to let a malformed value (a stale bookmarked URL, a
    hand-edited link) raise an unhandled ValueError and crash the request
    with a raw 500 instead of a clean, actionable 400 -- this is the one
    place that conversion happens now, so every caller gets the same
    clean failure instead of needing its own try/except."""
    try:
        return int(value)
    except (TypeError, ValueError):
        abort(400, description=f"'{field_name}' must be a whole number, got {value!r}")


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
        values = [_parse_int_arg(v, "decade") for v in str(decade).split(",") if v]
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
        params.append(_parse_int_arg(rating, "rating"))
    if rating_min:
        where.append("r.rating >= ?")
        params.append(_parse_int_arg(rating_min, "rating_min"))

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
    limit = min(_parse_int_arg(request.args.get("limit", 100), "limit"), 500)
    offset = _parse_int_arg(request.args.get("offset", 0), "offset")

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


# Some rippers embed covers well above 1000px on a side, several MB each --
# fine to keep on disk once, but wasteful to decode and transfer at full size
# for every display. ART_MAX_DIM covers the biggest on-screen art (HiFi
# theme's 190px frame) with headroom for high-DPI screens; ART_THUMB_DIM is
# for spots that render many covers at once (track list, mini now-playing
# bar), both 32-44px CSS but re-rendered on every scroll frame.
ART_MAX_DIM = 640
ART_THUMB_DIM = 128


def _resize_art(data, max_dim):
    """Downscale image bytes to fit within max_dim x max_dim, re-encoded as
    JPEG. Returns (bytes, mime), or (None, None) if Pillow isn't installed
    or the image can't be decoded -- callers fall back to the original
    bytes so a missing/broken Pillow never breaks art, just skips the
    resize."""
    try:
        from PIL import Image
    except ImportError:
        return None, None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue(), "image/jpeg"
    except Exception:
        return None, None


def get_art(track_id, fpath, thumb=False):
    """Cache-backed lookup of a track's embedded cover art on disk. `thumb`
    returns the small ART_THUMB_DIM re-encode, cached separately from the
    ART_MAX_DIM "full" copy so list scrolling never has to decode/transfer
    full-resolution art for a 32px row."""
    suffix = ".thumb" if thumb else ""
    for ext, mime in ((".jpg", "image/jpeg"), (".png", "image/png")):
        cached = os.path.join(ART_CACHE_DIR, f"{track_id}{suffix}{ext}")
        if os.path.isfile(cached):
            with open(cached, "rb") as f:
                return f.read(), mime

    # A full-size cover can be cached with no thumb alongside it yet --
    # not just a not-yet-thumbnailed embedded extraction (handled below),
    # but routinely now from _fetch_and_cache_art's external Deezer/iTunes/
    # MusicBrainz downloads, which cache a full-size image but never write
    # a thumb of their own. Derive the thumb from that cached full copy
    # before falling back to _extract_art, which reads embedded art from
    # the audio file itself -- exactly what external-only art has none of,
    # so that fallback would just fail and wrongly write a permanent .none
    # marker for a track whose art clearly does exist. Checked before the
    # .none check too, since a thumb request that raced an external fetch
    # (asked before the full copy existed, based its own miss on the audio
    # file alone) may have already written that same wrong .none marker --
    # a full copy existing now overrides it.
    if thumb:
        for ext, mime in ((".jpg", "image/jpeg"), (".png", "image/png")):
            full_cached = os.path.join(ART_CACHE_DIR, f"{track_id}{ext}")
            if os.path.isfile(full_cached):
                with open(full_cached, "rb") as f:
                    full_data = f.read()
                none_marker = os.path.join(ART_CACHE_DIR, f"{track_id}.none")
                if os.path.isfile(none_marker):
                    os.remove(none_marker)
                thumb_data, thumb_mime = _resize_art(full_data, ART_THUMB_DIM)
                if thumb_data is None:
                    return full_data, mime
                with open(os.path.join(ART_CACHE_DIR, f"{track_id}.thumb.jpg"), "wb") as tf:
                    tf.write(thumb_data)
                return thumb_data, thumb_mime

    if os.path.isfile(os.path.join(ART_CACHE_DIR, f"{track_id}.none")):
        return None, None

    data, mime = _extract_art(fpath)
    if not data:
        open(os.path.join(ART_CACHE_DIR, f"{track_id}.none"), "wb").close()
        return None, None

    full_data, full_mime = _resize_art(data, ART_MAX_DIM)
    if full_data is None:
        full_data, full_mime = data, mime
    full_ext = ".png" if full_mime == "image/png" else ".jpg"
    with open(os.path.join(ART_CACHE_DIR, f"{track_id}{full_ext}"), "wb") as f:
        f.write(full_data)

    thumb_data, thumb_mime = _resize_art(data, ART_THUMB_DIM)
    if thumb_data is not None:
        with open(os.path.join(ART_CACHE_DIR, f"{track_id}.thumb.jpg"), "wb") as f:
            f.write(thumb_data)

    if thumb:
        return (thumb_data, thumb_mime) if thumb_data is not None else (full_data, full_mime)
    return full_data, full_mime


@app.route("/api/art/<int:track_id>")
def art(track_id):
    db = get_db()
    row = db.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)
    fpath = os.path.join(MUSIC_DIR, row["path"])
    data, mime = get_art(track_id, fpath, thumb=request.args.get("thumb") == "1")
    if not data:
        abort(404)
    resp = make_response(data)
    resp.headers["Content-Type"] = mime
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


def _fetch_and_cache_art(conn, track_id, artist, title):
    """Best-effort cover art for a track with no embedded art, tried across
    several free, keyless sources in order (see art_lookup.py -- Deezer,
    then iTunes Search, then MusicBrainz+Cover Art Archive) since no single
    one of them has everything. Caches straight into ART_CACHE_DIR (the
    same place get_art already checks first) without ever touching the
    source audio file, so a bad match or a failed write can't corrupt
    anything. `conn` just needs execute()/commit() -- both the per-request
    g.db and a plain sqlite3.connect() (used by the iPod-import art
    backfill, which runs off a background thread with no request/g of its
    own) satisfy that. Returns (True, None) on success, else (False,
    error_message) -- callers doing this in bulk just check the bool and
    move on to the next track."""
    import art_lookup
    cover_url, _source = art_lookup.find_cover_url(artist, title)
    if not cover_url:
        return False, "No match found"

    import urllib.request
    try:
        with urllib.request.urlopen(cover_url, timeout=10) as resp:
            image_bytes = resp.read()
    except Exception:
        return False, "Couldn't download the image"

    dest = os.path.join(ART_CACHE_DIR, f"{track_id}.jpg")
    with open(dest, "wb") as f:
        f.write(image_bytes)
    none_marker = os.path.join(ART_CACHE_DIR, f"{track_id}.none")
    if os.path.isfile(none_marker):
        os.remove(none_marker)
    # The old thumb (if any) was re-encoded from whatever art -- or lack of
    # it -- existed before this fetch; drop it so get_art regenerates one
    # from the new cover on next request instead of serving a stale thumb.
    thumb_cached = os.path.join(ART_CACHE_DIR, f"{track_id}.thumb.jpg")
    if os.path.isfile(thumb_cached):
        os.remove(thumb_cached)
    conn.execute("UPDATE tracks SET has_art=1 WHERE id=?", (track_id,))
    conn.commit()
    return True, None


@app.route("/api/art/<int:track_id>/fetch", methods=["POST"])
def fetch_art(track_id):
    db = get_db()
    row = db.execute("SELECT artist, title FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)
    ok, error = _fetch_and_cache_art(db, track_id, row["artist"], row["title"])
    if not ok:
        status = 404 if error == "No match found" else 502
        return jsonify({"ok": False, "error": error}), status
    return jsonify({"ok": True, "track_id": track_id})


@app.route("/api/tracks/<int:track_id>/reveal", methods=["POST"])
def reveal_track(track_id):
    """Reveals a track's file in Finder, selected -- macOS only (matches
    ipod_staging_reveal's own platform guard above), purely a convenience
    that reads nothing and writes nothing."""
    db = get_db()
    row = db.execute("SELECT path FROM tracks WHERE id=?", (track_id,)).fetchone()
    if not row:
        abort(404)
    fpath = os.path.join(MUSIC_DIR, row["path"])
    if not os.path.isfile(fpath):
        return jsonify({"ok": False, "error": "File not found on disk"})
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", fpath])
    return jsonify({"ok": True})


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
    try:
        return _delete_track_rows_loop(db, rows, total, progress_cb, deleted, errors)
    finally:
        # Reached on a cancel (jobs.JobCancelled out of progress_cb) or any
        # failure partway: the rows for files already moved to trash must
        # still be committed, or the files and the library would disagree.
        db.commit()


def _delete_track_rows_loop(db, rows, total, progress_cb, deleted, errors):
    for i, row in enumerate(rows):
        fpath = os.path.join(MUSIC_DIR, row["path"])
        trash_name = f"{row['id']}_{os.path.basename(row['path'])}"
        trash_dest = os.path.join(TRASH_DIR, trash_name)
        try:
            if not os.path.isfile(fpath):
                raise OSError(f"Source file not found: {fpath}")
            os.makedirs(TRASH_DIR, exist_ok=True)
            safe_move(fpath, trash_dest)
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
        for ext in (".jpg", ".png", ".none", ".thumb.jpg", ".thumb.png"):
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


_delete_tracks_job = jobs.Job("delete_tracks", "Moving tracks to Trash", deleted=0, errors=None)
_delete_tracks_state = _delete_tracks_job.state


def _run_delete_tracks_bg(rows):
    progress_cb = _delete_tracks_job.progress

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")  # so deleted tracks take their ratings/playlist entries with them
        try:
            deleted, errors = _delete_track_rows(conn, rows, progress_cb=progress_cb)
            _delete_tracks_state["deleted"] = deleted
            _delete_tracks_state["errors"] = errors
        finally:
            conn.close()
    finally:
        _invalidate_dup_plan_cache()


@app.route("/api/delete-tracks", methods=["POST"])
def delete_tracks_route():
    """Move an explicit set of tracks to trash by id -- used by the
    duplicate review screen once the user has picked which version(s) to
    remove. Runs in the background with progress polling, same shape as
    every other bulk action here: when the music folder is on a different
    volume than the app's own data, each removal is a real cross-
    filesystem copy, not a fast rename -- for more than a few files that
    can take a real, visible amount of time, which a single blocking
    request with no progress showed as nothing but a static "Deleting…"
    for however long it took."""
    data = request.get_json(force=True, silent=True) or {}
    track_ids = data.get("track_ids") or []
    if not track_ids or not all(isinstance(t, int) for t in track_ids):
        abort(400)

    db = get_db()
    placeholders = ",".join("?" * len(track_ids))
    rows = [dict(r) for r in db.execute(
        f"SELECT id, path, artist, title, album FROM tracks WHERE id IN ({placeholders})", track_ids
    ).fetchall()]
    if not _delete_tracks_job.start(_run_delete_tracks_bg, rows, guard=_library_lock, prepare=lambda: close_db(None), total=len(rows)):
        return jsonify({"started": False, "error": "Already running"})
    return jsonify({"started": True, "total": len(rows)})


@app.route("/api/delete-tracks/progress")
def delete_tracks_progress():
    return jsonify(_delete_tracks_state)


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
    safe_move(row["trash_path"], dest)

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
    count = min(_parse_int_arg(request.args.get("count", 30), "count"), 100)
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
    name = (data.get("name") or "").strip()[:SMART_PLAYLIST_NAME_MAX_LEN]
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
    name = name[:PLAYLIST_NAME_MAX_LEN]
    db = get_db()
    cur = db.execute(
        "INSERT INTO playlists (name, created_at) VALUES (?, ?)",
        (name, datetime.datetime.utcnow().isoformat()),
    )
    db.commit()
    playlist_id = cur.lastrowid

    track_ids = data.get("track_ids") or []
    if track_ids:
        # An id that doesn't actually exist in tracks used to reach the
        # INSERT below and raise a raw sqlite3.IntegrityError (foreign key
        # violation) -- e.g. a stale id from a client that hasn't
        # refreshed since the last rescan removed the track. Filtering to
        # ids that actually exist keeps this a normal, silent no-op for
        # those instead of a 500 for the whole request.
        placeholders = ",".join("?" * len(track_ids))
        valid_ids = {
            r[0] for r in db.execute(
                f"SELECT id FROM tracks WHERE id IN ({placeholders})", track_ids
            ).fetchall()
        }
        for i, tid in enumerate(t for t in track_ids if t in valid_ids):
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
    if not db.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
        abort(404)
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
    # A nonexistent playlist_id or track_id used to reach the INSERT below
    # and raise a raw sqlite3.IntegrityError (foreign key violation)
    # instead of a clean 404/no-op -- e.g. a playlist deleted in another
    # tab, or a stale track id from a client that hasn't refreshed since
    # the last rescan removed the track.
    if not db.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone():
        abort(404)
    placeholders = ",".join("?" * len(track_ids))
    valid_ids = {
        r[0] for r in db.execute(
            f"SELECT id FROM tracks WHERE id IN ({placeholders})", track_ids
        ).fetchall()
    }
    track_ids = [t for t in track_ids if t in valid_ids]
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


def _fingerprint_lookup(fpath, api_key, fpcalc_path, timeout=30):
    """Runs Chromaprint (fpcalc) against a local audio file and looks the
    fingerprint up on AcoustID -- the free, open, MusicBrainz-backed
    fingerprint database tools like Picard use. Shared by radio_identify
    (fingerprints a live-captured clip) and the library audio-verification
    feature below (fingerprints a file already on disk) -- the "read
    fpcalc's json, call AcoustID, pick the best-scoring match" logic is
    identical either way; only how the audio bytes get there differs.
    Returns (artist, title, score) for the highest-confidence match with
    both a title and at least one credited artist, or (None, None, None)
    if fpcalc or the lookup itself found nothing usable. Raises on a
    genuine I/O/network error -- callers decide how to report that."""
    result = subprocess.run([fpcalc_path, "-json", fpath], capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0 or not result.stdout:
        return None, None, None
    fp_data = json.loads(result.stdout)

    params = {
        "client": api_key,
        "fingerprint": fp_data["fingerprint"],
        "duration": int(fp_data["duration"]),
        "meta": "recordings",
    }
    lookup_url = "https://api.acoustid.org/v2/lookup?" + urllib.parse.urlencode(params)
    lookup_req = urllib.request.Request(lookup_url, headers={"User-Agent": RADIO_UA})
    with urllib.request.urlopen(lookup_req, timeout=15) as resp:
        lookup = json.loads(resp.read().decode("utf-8"))

    if lookup.get("status") != "ok":
        msg = (lookup.get("error") or {}).get("message", "Lookup failed")
        raise RuntimeError(msg)

    results = lookup.get("results", [])
    if not results:
        return None, None, None
    best_score = max(r.get("score", 0) for r in results)

    # One AcoustID "result" (one fingerprint match, one score) is routinely
    # linked to several different MusicBrainz recordings -- other users'
    # separate submissions of the same actual song, not a confidence
    # ranking of its own. Taking whichever recording happened to be listed
    # first isn't picking the best match, it's picking an arbitrary
    # submission order: this returned "Man Made" for a real A Flock of
    # Seagulls file that is unambiguously "Wishing (...)" -- 5 of the 6
    # recordings tied to that one 0.987-confidence result agreed it was
    # "Wishing", only 1 said "Man Made", and the old first-wins logic
    # happened to hit that lone wrong one first. Majority vote instead,
    # among only the recordings tied to the top score.
    def norm(s):
        return re.sub(r"[^\w]", "", (s or "").lower())

    votes = {}  # (norm(artist), norm(title)) -> [count, artist, title]
    for r in results:
        if r.get("score", 0) != best_score:
            continue
        for rec in r.get("recordings", []):
            title, artists = rec.get("title"), rec.get("artists")
            if not title or not artists:
                continue
            artist = ", ".join(a["name"] for a in artists if a.get("name"))
            key = (norm(artist), norm(title))
            entry = votes.setdefault(key, [0, artist, title])
            entry[0] += 1

    if not votes:
        return None, None, None
    _count, artist, title = max(votes.values(), key=lambda v: v[0])
    return artist, title, best_score


@app.route("/api/radio/identify", methods=["POST"])
def radio_identify():
    """Shazam-style "what's this song" for a station whose ICY metadata is
    missing, empty, or just repeats the station's own name/slogan.
    Fingerprints ~15s of live audio and looks it up via _fingerprint_lookup.
    No per-query cost, nothing stored; the captured clip is a temp file
    deleted right after."""
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

        try:
            artist, title, score = _fingerprint_lookup(tmp_path, api_key, fpcalc_path, timeout=20)
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)})
        if title is None:
            return jsonify({"ok": False, "error": "no_match"})
        return jsonify({"ok": True, "artist": artist, "title": title, "score": score})
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
