"""Tracks *which* library is open -- separate from config.py (app-wide
preferences: theme, wood finish, etc, untouched by any of this) and
scan_library.py (the scan algorithm itself, unchanged).

A library is one SQLite file (LIBRARY_EXT) the user names and saves
wherever they like -- an external drive, anywhere. Music files stay put;
only the catalog (tags, playlists, ratings, that file's own music_dir
pointer) lives in the file. Everything about "which library, and what does
it point at" is config.json's `current_library` (a path) and
`recent_libraries` (a small MRU list of {path, companion_dir_override}).

Every other module in this codebase (scan_library.py, organize_by_artist.py,
fill_genres.py, fill_years.py, unify_artist_genre.py, fix_artist_title.py,
and app.py itself) already resolves its own MUSIC_DIR/DB_PATH globals from
the JUKEBOX_MUSIC_DIR/JUKEBOX_DB_PATH env vars, re-read fresh on every
importlib.reload() app.py already does before using any of them. That
existing convention is why switching libraries doesn't need to touch any of
those modules -- resolve_startup() (called once, before app.py computes its
own globals) and app.py's own _switch_library() (called on every runtime
switch) only ever need to update those same two env vars, and the rest
follows for free.
"""
import os
import sqlite3

import config as jukebox_config
from fs_safety import safe_move

LIBRARY_EXT = ".nbpmlib"
MAX_RECENT = 10


def display_name(db_path):
    """Derived from the filename, not a separately-stored field -- so
    renaming the file in Finder just works, with nothing to keep in sync."""
    return os.path.splitext(os.path.basename(db_path))[0]


def _default_legacy_db_path():
    return os.environ.get("JUKEBOX_DB_PATH") or os.path.join(jukebox_config.get_app_data_dir(), "library.db")


def read_music_dir(db_path):
    if not db_path or not os.path.isfile(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS library_meta (key TEXT PRIMARY KEY, value TEXT)")
        row = conn.execute("SELECT value FROM library_meta WHERE key='music_dir'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def write_music_dir(db_path, new_dir):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS library_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO library_meta (key, value) VALUES ('music_dir', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (new_dir,),
        )
        conn.commit()
    finally:
        conn.close()


def companion_dir(db_path):
    """Where this library's own trash/art_cache/backups live. A freshly
    created library gets a folder right alongside its own file (self-
    contained, portable together) -- except the legacy library migrated
    in from before multi-library support existed, which keeps using
    today's fixed app-data location via its recorded override, so the
    user's existing trash/art cache/backups are never moved. This isn't
    just tidiness: art_cache files are named by track id, and track ids
    are autoincrement primary keys local to each library's own DB, so two
    different libraries WILL have colliding ids -- sharing a cache across
    libraries would show the wrong album art."""
    cfg = jukebox_config.load_config()
    for entry in cfg.get("recent_libraries", []):
        if entry.get("path") == db_path and entry.get("companion_dir_override"):
            return entry["companion_dir_override"]
    base, _ext = os.path.splitext(db_path)
    return base + ".nbpmdata"


def migrate_stray_companion_dir(db_path):
    """One-time repair for a real bug: app.py used to compute a library's
    ART_CACHE_DIR/TRASH_DIR/BACKUP_DIR globals at startup as plain
    os.path.dirname(db_path)/{art_cache,trash,backups}, ignoring this
    module's own companion_dir() entirely -- unlike _switch_library()
    (app.py), which always used companion_dir() correctly. Only matters
    for a non-legacy library: the legacy library's companion_dir IS that
    same bare app-data folder, by its recorded override, so old and new
    locations coincide there and this is a no-op. For any other library
    (anything created after multi-library support existed), that bug
    meant its cache/trash/backups landed in a bare folder dropped next to
    the library file instead of its own isolated <name>.nbpmdata/ folder
    -- so a second library saved in the same directory would collide
    with it (art_cache files are named by track id, and ids are
    autoincrement per-DB, so two libraries WILL reuse the same ids).
    Moves each subfolder's contents into the correct location, file by
    file, never overwriting anything already there -- safe to call on
    every startup, and a no-op once nothing's left to move. Trash entries
    also get their DB-recorded trash_path repointed at the new location
    (the only one of the three that's tracked anywhere besides the
    filesystem itself -- art_cache/backups are found by listing the
    directory, but restoring/purging a trashed file looks it up by the
    absolute path recorded in the library's own `trash` table at the
    moment it was trashed)."""
    correct = companion_dir(db_path)
    stray = os.path.dirname(db_path)
    if os.path.normpath(correct) == os.path.normpath(stray):
        return
    moved_trash = []
    for name in ("trash", "art_cache", "backups"):
        stray_sub = os.path.join(stray, name)
        if not os.path.isdir(stray_sub):
            continue
        correct_sub = os.path.join(correct, name)
        os.makedirs(correct_sub, exist_ok=True)
        for fname in os.listdir(stray_sub):
            src = os.path.join(stray_sub, fname)
            dst = os.path.join(correct_sub, fname)
            if os.path.exists(dst):
                continue  # already there -- leave the stray copy alone
            try:
                safe_move(src, dst)
            except OSError:
                continue  # left in place; picked up again on the next launch
            if name == "trash":
                moved_trash.append((src, dst))
        try:
            os.rmdir(stray_sub)  # only succeeds once truly empty
        except OSError:
            pass

    if moved_trash and os.path.isfile(db_path):
        conn = sqlite3.connect(db_path)
        try:
            for old_path, new_path in moved_trash:
                conn.execute("UPDATE trash SET trash_path=? WHERE trash_path=?", (new_path, old_path))
            conn.commit()
        except sqlite3.OperationalError:
            pass  # no trash table yet (a library that's never been scanned)
        finally:
            conn.close()


def _add_recent(path, companion_dir_override=None):
    def _mutate(cfg):
        recents = cfg.setdefault("recent_libraries", [])
        existing = next((r for r in recents if r.get("path") == path), None)
        # Preserve an existing override (only ever set once, at migration
        # time) across ordinary re-opens/switches, which don't pass one.
        override = companion_dir_override if companion_dir_override is not None else (
            existing.get("companion_dir_override") if existing else None
        )
        recents[:] = [r for r in recents if r.get("path") != path]
        recents.insert(0, {"path": path, "companion_dir_override": override})
        del recents[MAX_RECENT:]
    jukebox_config.update_config(_mutate)


def _set_current(path):
    jukebox_config.update_config(lambda cfg: cfg.__setitem__("current_library", path))


def list_recent():
    cfg = jukebox_config.load_config()
    return [
        {"path": r["path"], "name": display_name(r["path"]), "exists": os.path.isfile(r["path"])}
        for r in cfg.get("recent_libraries", [])
    ]


def get_current():
    cfg = jukebox_config.load_config()
    path = cfg.get("current_library")
    if not path:
        return None
    return {
        "path": path,
        "name": display_name(path),
        "music_dir": read_music_dir(path) or cfg.get("music_dir"),
    }


def create_new(save_path, music_dir):
    """Builds a fresh library at save_path (schema via scan_library's own
    build_schema, same lazy-migration convention every table there already
    uses), records its music_dir, creates its companion data folders, and
    registers it as current."""
    import scan_library

    if not save_path.endswith(LIBRARY_EXT):
        save_path += LIBRARY_EXT
    conn = sqlite3.connect(save_path)
    try:
        scan_library.build_schema(conn)
    finally:
        conn.close()
    write_music_dir(save_path, music_dir)
    comp = companion_dir(save_path)
    for sub in ("trash", "art_cache", "backups"):
        os.makedirs(os.path.join(comp, sub), exist_ok=True)
    _add_recent(save_path)
    _set_current(save_path)
    return save_path


def open_existing(path):
    """Validates the file, reads its stored music_dir, and registers it as
    current -- callers (app.py's _switch_library) still have to do the
    actual runtime env-var/global switch; this only handles the config/
    metadata bookkeeping side."""
    if not os.path.isfile(path):
        return {"ok": False, "error": f"Couldn't find “{os.path.basename(path)}” — is the drive connected?"}
    music_dir = read_music_dir(path)
    _add_recent(path)
    _set_current(path)
    return {"ok": True, "path": path, "music_dir": music_dir}


def _migrate_legacy_if_needed():
    """One-time, idempotent: if config has no current_library yet, treat
    the existing fixed-path library.db as "library #1" so it keeps working
    exactly as it always has -- including where its trash/art cache/backups
    live, recorded as an explicit override rather than moved anywhere."""
    def _migrate(cfg):
        if "current_library" in cfg:
            return
        legacy_db_path = _default_legacy_db_path()
        cfg["current_library"] = legacy_db_path
        cfg["recent_libraries"] = [{
            "path": legacy_db_path,
            "companion_dir_override": jukebox_config.get_app_data_dir(),
        }]
    jukebox_config.update_config(_migrate)

    cfg = jukebox_config.load_config()
    current = cfg["current_library"]
    # The existing library.db already has real data the first time this
    # runs post-update -- give it its own library_meta record so it
    # behaves like any other library from here on, instead of leaning on
    # the flat (pre-multi-library) music_dir config key forever. Only
    # writes once: a second call finds read_music_dir already non-empty.
    if os.path.isfile(current) and cfg.get("music_dir") and not read_music_dir(current):
        write_music_dir(current, cfg["music_dir"])


def resolve_startup():
    """Decides which library is current and makes sure JUKEBOX_DB_PATH/
    JUKEBOX_MUSIC_DIR reflect it, before app.py (and everything it
    reloads) computes its own globals from those same two env vars. An
    already-set JUKEBOX_DB_PATH -- the dev/test override every module in
    this codebase already respects -- is left completely alone; this only
    resolves things when nothing more specific was already supplied."""
    if os.environ.get("JUKEBOX_DB_PATH"):
        return os.environ["JUKEBOX_DB_PATH"], os.environ.get("JUKEBOX_MUSIC_DIR")

    _migrate_legacy_if_needed()

    cfg = jukebox_config.load_config()
    current = cfg.get("current_library")
    if not current:
        return None, None  # truly first-ever launch, no library.db exists yet

    music_dir = read_music_dir(current) or cfg.get("music_dir")
    os.environ["JUKEBOX_DB_PATH"] = current
    if music_dir:
        os.environ["JUKEBOX_MUSIC_DIR"] = music_dir
    return current, music_dir
