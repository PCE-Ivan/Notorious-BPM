"""Change journal: every bulk tag edit and file move, recorded so it can be undone.

Until now a bulk tool (fix artist & track names, unify genres, fix years,
fill genres, organize by artist) wrote straight into your audio files and
the only safety net was a snapshot of the *database*. Restoring that put
the library index back, but the files kept their new tags -- the two then
disagreed. The journal records what each operation actually changed
(old value -> new value, old path -> new path) in the library's own
database, so "Undo" puts the files and the index back together.

Shape:
  operations(id, kind, label, started_at, finished_at, item_count, undone, batch)
  operation_items(op_id, track_id, kind 'tag'|'move', field, old/new value, old/new path)

Writers don't call this directly: tagio.write_tags() reports tag changes
through record_tag(), which does nothing unless the calling thread is inside
an `operation(...)` block -- so a one-off write outside any operation (or
an undo) is simply not journaled. record_*() never raises; a journal
failure must not fail the edit it was trying to describe.
"""
import contextlib
import datetime
import logging
import os
import sqlite3
import threading

log = logging.getLogger("jukebox.journal")

KEEP_OPERATIONS = 100
BATCH_WINDOW_SECONDS = 15 * 60  # edits sharing a batch id within this window join one operation

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    undone INTEGER NOT NULL DEFAULT 0,
    batch TEXT
);
CREATE TABLE IF NOT EXISTS operation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id INTEGER NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    track_id INTEGER,
    kind TEXT NOT NULL,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    old_path TEXT,
    new_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_operation_items_op ON operation_items(op_id);
"""

_local = threading.local()


def _now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds")


def _connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    return conn


class _Active:
    def __init__(self, conn, op_id, music_dir):
        self.conn, self.op_id, self.music_dir = conn, op_id, music_dir


@contextlib.contextmanager
def operation(db_path, music_dir, kind, label, batch=None):
    """Everything tagio/record_move report on this thread inside the block
    is filed under one operation. With `batch`, repeated blocks carrying the
    same batch id (the front end sends one per "Apply" click, then one
    request per track) join the same operation instead of each making their
    own. An operation that recorded nothing leaves no trace."""
    conn = _connect(db_path)
    op_id = None
    created = False
    try:
        if batch:
            row = conn.execute(
                "SELECT id FROM operations WHERE batch=? AND undone=0 AND "
                "started_at >= ? ORDER BY id DESC LIMIT 1",
                (batch, (datetime.datetime.utcnow() - datetime.timedelta(seconds=BATCH_WINDOW_SECONDS)).isoformat(timespec="seconds")),
            ).fetchone()
            if row:
                op_id = row["id"]
        if op_id is None:
            op_id = conn.execute(
                "INSERT INTO operations (kind, label, started_at, batch) VALUES (?,?,?,?)",
                (kind, label, _now(), batch),
            ).lastrowid
            created = True
            conn.commit()
        previous = getattr(_local, "active", None)
        _local.active = _Active(conn, op_id, music_dir)
        try:
            yield op_id
        finally:
            _local.active = previous
            count = conn.execute("SELECT COUNT(*) FROM operation_items WHERE op_id=?", (op_id,)).fetchone()[0]
            if count == 0 and created:
                conn.execute("DELETE FROM operations WHERE id=?", (op_id,))
            else:
                conn.execute("UPDATE operations SET item_count=?, finished_at=? WHERE id=?", (count, _now(), op_id))
            conn.commit()
            _prune(conn)
    finally:
        conn.close()


def _insert(active, track_id, kind, field=None, old=None, new=None, old_path=None, new_path=None):
    active.conn.execute(
        "INSERT INTO operation_items (op_id, track_id, kind, field, old_value, new_value, old_path, new_path) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (active.op_id, track_id, kind, field, old, new, old_path, new_path),
    )
    active.conn.commit()


def record_tag(fpath, field, old, new):
    active = getattr(_local, "active", None)
    if not active:
        return
    try:
        rel = os.path.relpath(fpath, active.music_dir)
        row = active.conn.execute("SELECT id FROM tracks WHERE path=?", (rel,)).fetchone()
        _insert(active, row["id"] if row else None, "tag", field=field, old=old, new=new, old_path=rel)
    except Exception:
        log.exception("Could not journal tag change for %s", fpath)


def record_moves(path_moves):
    """path_moves: {old_relative_path: new_relative_path}. Call after the
    library index has been repointed, so each can be matched to its track
    id by its new path."""
    active = getattr(_local, "active", None)
    if not active or not path_moves:
        return
    try:
        for old, new in path_moves.items():
            row = active.conn.execute("SELECT id FROM tracks WHERE path=?", (new,)).fetchone()
            active.conn.execute(
                "INSERT INTO operation_items (op_id, track_id, kind, old_path, new_path) VALUES (?,?,?,?,?)",
                (active.op_id, row["id"] if row else None, "move", old, new),
            )
        active.conn.commit()
    except Exception:
        log.exception("Could not journal file moves")


def _prune(conn):
    try:
        conn.execute(
            "DELETE FROM operations WHERE id NOT IN (SELECT id FROM operations ORDER BY id DESC LIMIT ?)",
            (KEEP_OPERATIONS,),
        )
        conn.commit()
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------- reading
def list_operations(db_path, limit=40):
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, label, started_at, finished_at, item_count, undone FROM operations "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------------------ undo
_FIELD_COLUMNS = {
    "genre": ("genre", "primary_genre"),
    "artist": ("artist",),
    "title": ("title",),
    "album": ("album",),
}


def undo(db_path, music_dir, op_id, progress=None):
    """Reverses one operation, newest change first. A change is only
    reversed if the file still holds the value that operation wrote -- if
    you've edited that tag since, your newer edit wins and the item is
    reported as skipped. Returns {"restored", "skipped", "errors"}."""
    import tagio
    from fs_safety import safe_move

    conn = _connect(db_path)
    restored = skipped = errors = 0
    try:
        op = conn.execute("SELECT id, undone FROM operations WHERE id=?", (op_id,)).fetchone()
        if not op:
            raise ValueError("No such operation")
        items = conn.execute(
            "SELECT * FROM operation_items WHERE op_id=? ORDER BY id DESC", (op_id,)).fetchall()
        total = len(items)
        for i, it in enumerate(items):
            try:
                if it["kind"] == "tag":
                    outcome = _undo_tag(conn, music_dir, it, tagio)
                else:
                    outcome = _undo_move(conn, music_dir, it, safe_move)
            except OSError as e:
                log.warning("Undo item failed: %s", e)
                outcome = "error"
            if outcome == "restored":
                restored += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                errors += 1
            if progress:
                progress(i + 1, total)
        conn.commit()
        if errors == 0:
            conn.execute("UPDATE operations SET undone=1 WHERE id=?", (op_id,))
            conn.commit()
    finally:
        conn.close()
    return {"restored": restored, "skipped": skipped, "errors": errors}


def _current_path(conn, it):
    if it["track_id"] is not None:
        row = conn.execute("SELECT path FROM tracks WHERE id=?", (it["track_id"],)).fetchone()
        if row:
            return row["path"]
    return None


def _undo_tag(conn, music_dir, it, tagio):
    rel = _current_path(conn, it)
    if rel is None:
        return "skipped"  # the track is gone from the library
    fpath = os.path.join(music_dir, rel)
    if not os.path.isfile(fpath):
        return "skipped"
    if tagio.read_tag(fpath, it["field"]) != it["new_value"]:
        return "skipped"  # edited again since -- leave the newer value alone
    if not tagio.write_tag(fpath, it["field"], it["old_value"], record=False):
        return "error"
    old = it["old_value"]
    if it["field"] == "year":
        year = None
        if old:
            try:
                year = int(str(old)[:4])
            except ValueError:
                year = None
        conn.execute("UPDATE tracks SET year=?, decade=? WHERE id=?",
                     (year, (year // 10) * 10 if year else None, it["track_id"]))
    elif it["field"] in _FIELD_COLUMNS:
        for column in _FIELD_COLUMNS[it["field"]]:
            conn.execute(f"UPDATE tracks SET {column}=? WHERE id=?", (old, it["track_id"]))
    return "restored"


def _undo_move(conn, music_dir, it, safe_move):
    rel = _current_path(conn, it)
    if rel != it["new_path"]:
        return "skipped"  # moved again (or removed) since
    src = os.path.join(music_dir, it["new_path"])
    dst = os.path.join(music_dir, it["old_path"])
    if not os.path.isfile(src) or os.path.exists(dst):
        return "skipped"
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    safe_move(src, dst)
    conn.execute("UPDATE tracks SET path=? WHERE id=?", (it["old_path"], it["track_id"]))
    return "restored"
