"""Library health: compares the database against the disk and the caches,
reports what disagrees, and repairs the safe cases on request.

Every inconsistency this app has actually had in practice is a check here:
a track whose art flag says "has art" while no image exists anywhere (found
on a real library), stale ".none" markers shadowing art that does exist,
trash records pointing at files that moved, a bare art_cache/trash/backups
folder left beside a library that should have used its own companion
folder, and playlist/rating rows left behind by deletes made on a
connection without foreign keys switched on.

Read-only: run_checks() never changes anything. repair() is the only
writer, takes an explicit list of repair ids, and every repair is limited
to the clearly-safe fix for its own kind of problem -- anything that would
need judgement (files in the trash folder nobody references, a damaged
database) is reported, never auto-"fixed".
"""
import os
import re
import sqlite3
import time

ART_FILE_RE = re.compile(r"^(\d+)(?:\.thumb)?\.(jpg|png|none)$")
MISSING_FILES_REPAIR_LIMIT = 0.2  # same ">20% missing = probably an unplugged drive" guard the scan uses


def _issue(issue_id, severity, title, detail, count, examples=None, repair=None, repair_label=None):
    return {
        "id": issue_id, "severity": severity, "title": title, "detail": detail,
        "count": count, "examples": (examples or [])[:8], "repair": repair, "repair_label": repair_label,
    }


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _art_cache_index(art_cache_dir):
    """{track_id: set of suffix kinds present} for what's on disk right now."""
    index = {}
    try:
        names = os.listdir(art_cache_dir)
    except OSError:
        return index
    for name in names:
        m = ART_FILE_RE.match(name)
        if not m:
            continue
        index.setdefault(int(m.group(1)), set()).add(name)
    return index


def run_checks(db_path, music_dir, art_cache_dir, trash_dir, backup_dir,
               stray_dir=None, correct_dir=None, has_embedded_art=None, progress=None):
    """Returns {"issues": [...], "healthy": bool, "checked_at": epoch,
    "tracks": n}. `progress(done, total)` is called between checks (and may
    raise to cancel); `has_embedded_art(path)` lets the caller supply the
    tag-reading half of the art-flag check without this module depending on
    mutagen."""
    steps = 7
    done = 0

    def tick():
        nonlocal done
        done += 1
        if progress:
            progress(done, steps)

    issues = []
    conn = _connect(db_path)
    try:
        # ---------------------------------------------------- database itself
        quick = conn.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            issues.append(_issue(
                "db_integrity", "critical", "The library database reports damage",
                f"SQLite says: {quick}. Restore the newest snapshot from the library's backups folder "
                "(one is taken before every bulk change).", 1))
        tick()

        rows = conn.execute("SELECT id, path, artist, title, has_art FROM tracks").fetchall()
        track_ids = {r["id"] for r in rows}
        total_tracks = len(rows)

        # ------------------------------------------------------ files on disk
        music_ok = bool(music_dir) and os.path.isdir(music_dir)
        missing = []
        if music_ok:
            for i, r in enumerate(rows):
                if not os.path.exists(os.path.join(music_dir, r["path"])):
                    missing.append(r)
                if progress and i % 2000 == 0:
                    progress(done, steps)
        if music_dir and not music_ok:
            issues.append(_issue(
                "music_unreachable", "warning", "Music folder isn't reachable",
                f"{music_dir} can't be read — if it's on an external drive, reconnect it. File checks were skipped.", 1))
        elif missing:
            ratio = len(missing) / max(1, total_tracks)
            examples = [f"{r['artist'] or '?'} — {r['title'] or r['path']}" for r in missing]
            if ratio > MISSING_FILES_REPAIR_LIMIT:
                issues.append(_issue(
                    "missing_files", "warning", "Many tracks point at files that aren't there",
                    f"{len(missing):,} of {total_tracks:,} tracks ({ratio:.0%}). That many usually means a drive is "
                    "disconnected or asleep, not that the files are gone — reconnect it and check again before "
                    "removing anything.", len(missing), examples))
            else:
                issues.append(_issue(
                    "missing_files", "warning", "Tracks whose files are gone",
                    "These library entries point at files that no longer exist on disk.", len(missing), examples,
                    repair="remove_missing_tracks", repair_label=f"Remove {len(missing):,} dead entries"))
        tick()

        # ---------------------------------------------------------- cover art
        index = _art_cache_index(art_cache_dir)
        orphan_art = sorted(tid for tid in index if tid not in track_ids)
        if orphan_art:
            issues.append(_issue(
                "art_orphans", "info", "Cover-art files for tracks that no longer exist",
                "Leftovers from deleted tracks; safe to remove.", len(orphan_art),
                [f"track #{t}" for t in orphan_art], repair="delete_orphan_art",
                repair_label=f"Delete {len(orphan_art):,} leftover art files"))

        stale_none = sorted(
            tid for tid, names in index.items()
            if tid in track_ids and any(n.endswith(".none") for n in names)
            and any(n.endswith((".jpg", ".png")) and ".thumb" not in n for n in names)
        )
        if stale_none:
            issues.append(_issue(
                "art_stale_none", "warning", "“No art” markers hiding art that exists",
                "A marker saying a track has no cover art sits next to a cached image — the app would keep "
                "treating the track as art-less.", len(stale_none), [f"track #{t}" for t in stale_none],
                repair="clear_stale_none", repair_label=f"Clear {len(stale_none):,} stale markers"))
        tick()

        flag_wrong_yes = []  # flagged has_art=1 but no cached image and none embedded
        flag_wrong_no = []   # flagged has_art=0 but a cached image exists
        for r in rows:
            cached = any(n.endswith((".jpg", ".png")) and ".thumb" not in n for n in index.get(r["id"], ()))
            if r["has_art"] and not cached:
                if has_embedded_art is None or not music_ok:
                    continue
                if not has_embedded_art(os.path.join(music_dir, r["path"])):
                    flag_wrong_yes.append(r)
            elif not r["has_art"] and cached:
                flag_wrong_no.append(r)
        if flag_wrong_yes:
            issues.append(_issue(
                "art_flag_missing_image", "warning", "Marked as having cover art, but there's none",
                "The library believes these tracks have art, but there's no cached image and nothing embedded "
                "in the file — so they never get picked up by “Find missing cover art”.", len(flag_wrong_yes),
                [f"{r['artist'] or '?'} — {r['title'] or r['path']}" for r in flag_wrong_yes],
                repair="reset_art_flags", repair_label=f"Mark {len(flag_wrong_yes):,} tracks as missing art"))
        if flag_wrong_no:
            issues.append(_issue(
                "art_flag_unset", "info", "Has cover art, but not marked as such",
                "A cached image exists for these tracks but the library doesn't know.", len(flag_wrong_no),
                [f"{r['artist'] or '?'} — {r['title'] or r['path']}" for r in flag_wrong_no],
                repair="set_art_flags", repair_label=f"Mark {len(flag_wrong_no):,} tracks as having art"))
        tick()

        # -------------------------------------------------------------- trash
        trash_rows = conn.execute("SELECT id, trash_path, artist, title FROM trash").fetchall()
        dangling_trash = [t for t in trash_rows if not os.path.isfile(t["trash_path"])]
        if dangling_trash:
            issues.append(_issue(
                "trash_dangling", "warning", "Trash entries whose file is missing",
                "Restoring these would fail; the file isn't where the library recorded it.", len(dangling_trash),
                [f"{t['artist'] or '?'} — {t['title'] or t['trash_path']}" for t in dangling_trash],
                repair="delete_dangling_trash", repair_label=f"Remove {len(dangling_trash):,} dead trash entries"))
        referenced = {os.path.normpath(t["trash_path"]) for t in trash_rows}
        unreferenced = []
        try:
            for name in os.listdir(trash_dir):
                full = os.path.normpath(os.path.join(trash_dir, name))
                if os.path.isfile(full) and full not in referenced and not name.startswith("."):
                    unreferenced.append(name)
        except OSError:
            pass
        if unreferenced:
            issues.append(_issue(
                "trash_untracked", "info", "Files in the Trash folder the library doesn't know about",
                "Left alone on purpose — they might be something you put there yourself. Open the folder to decide.",
                len(unreferenced), unreferenced))
        tick()

        # ----------------------------------------------- links between tables
        dangling_pl = conn.execute(
            "SELECT COUNT(*) FROM playlist_tracks WHERE track_id NOT IN (SELECT id FROM tracks) "
            "OR playlist_id NOT IN (SELECT id FROM playlists)").fetchone()[0]
        dangling_ratings = conn.execute(
            "SELECT COUNT(*) FROM ratings WHERE track_id NOT IN (SELECT id FROM tracks)").fetchone()[0]
        if dangling_pl or dangling_ratings:
            issues.append(_issue(
                "dangling_links", "info", "Playlist or rating entries for tracks that are gone",
                f"{dangling_pl:,} playlist entries and {dangling_ratings:,} ratings point at deleted tracks "
                "(deletes made without foreign keys switched on don't cascade).", dangling_pl + dangling_ratings,
                repair="delete_dangling_links", repair_label="Remove the orphaned entries"))
        tick()

        # --------------------------------------------- folders & snapshots
        if stray_dir and correct_dir and os.path.normpath(stray_dir) != os.path.normpath(correct_dir):
            stray = [n for n in ("art_cache", "trash", "backups") if os.path.isdir(os.path.join(stray_dir, n))]
            if stray:
                issues.append(_issue(
                    "stray_companion", "warning", "Cache/trash/backup folders in the wrong place",
                    f"{', '.join(stray)} sit beside the library file instead of inside its own companion folder.",
                    len(stray), stray, repair="migrate_stray_dirs", repair_label="Move them into place"))
        newest = None
        try:
            snaps = [f for f in os.listdir(backup_dir) if f.startswith("library-") and f.endswith(".db")]
            if snaps:
                newest = max(os.path.getmtime(os.path.join(backup_dir, f)) for f in snaps)
        except OSError:
            pass
        if newest is None:
            issues.append(_issue(
                "no_snapshot", "info", "No library snapshot yet",
                "A snapshot is taken automatically before any bulk change; none exists yet.", 1))
        tick()
    finally:
        conn.close()

    order = {"critical": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order[i["severity"]])
    return {
        "issues": issues,
        "healthy": not any(i["severity"] != "info" for i in issues),
        "checked_at": time.time(),
        "tracks": total_tracks,
    }


# ------------------------------------------------------------------- repairs
def repair(repair_ids, db_path, music_dir, art_cache_dir, trash_dir, has_embedded_art=None,
           migrate_stray=None):
    """Applies the requested repairs; returns {repair_id: number_fixed}.
    Re-derives what to fix from the live state each time (never trusts a
    client-sent list of rows), so a repair can only ever act on something
    that is wrong right now."""
    results = {}
    conn = _connect(db_path)
    try:
        for rid in repair_ids:
            if rid == "remove_missing_tracks":
                if not (music_dir and os.path.isdir(music_dir)):
                    results[rid] = 0
                    continue
                rows = conn.execute("SELECT id, path FROM tracks").fetchall()
                gone = [r["id"] for r in rows if not os.path.exists(os.path.join(music_dir, r["path"]))]
                if len(gone) > max(1, len(rows)) * MISSING_FILES_REPAIR_LIMIT:
                    results[rid] = 0  # refuse: looks like an unplugged drive, not deleted files
                    continue
                conn.executemany("DELETE FROM tracks WHERE id=?", [(i,) for i in gone])
                results[rid] = len(gone)
            elif rid == "delete_orphan_art":
                ids = {r[0] for r in conn.execute("SELECT id FROM tracks")}
                removed = 0
                for name in os.listdir(art_cache_dir):
                    m = ART_FILE_RE.match(name)
                    if m and int(m.group(1)) not in ids:
                        os.remove(os.path.join(art_cache_dir, name))
                        removed += 1
                results[rid] = removed
            elif rid == "clear_stale_none":
                index = _art_cache_index(art_cache_dir)
                removed = 0
                for tid, names in index.items():
                    none = f"{tid}.none"
                    if none in names and any(n.endswith((".jpg", ".png")) and ".thumb" not in n for n in names):
                        os.remove(os.path.join(art_cache_dir, none))
                        removed += 1
                results[rid] = removed
            elif rid in ("reset_art_flags", "set_art_flags"):
                index = _art_cache_index(art_cache_dir)
                rows = conn.execute("SELECT id, path, has_art FROM tracks").fetchall()
                changed = 0
                for r in rows:
                    cached = any(n.endswith((".jpg", ".png")) and ".thumb" not in n for n in index.get(r["id"], ()))
                    if rid == "reset_art_flags" and r["has_art"] and not cached:
                        if has_embedded_art is not None and music_dir and has_embedded_art(os.path.join(music_dir, r["path"])):
                            continue
                        conn.execute("UPDATE tracks SET has_art=0 WHERE id=?", (r["id"],))
                        changed += 1
                    elif rid == "set_art_flags" and not r["has_art"] and cached:
                        conn.execute("UPDATE tracks SET has_art=1 WHERE id=?", (r["id"],))
                        changed += 1
                results[rid] = changed
            elif rid == "delete_dangling_trash":
                rows = conn.execute("SELECT id, trash_path FROM trash").fetchall()
                gone = [r["id"] for r in rows if not os.path.isfile(r["trash_path"])]
                conn.executemany("DELETE FROM trash WHERE id=?", [(i,) for i in gone])
                results[rid] = len(gone)
            elif rid == "delete_dangling_links":
                a = conn.execute(
                    "DELETE FROM playlist_tracks WHERE track_id NOT IN (SELECT id FROM tracks) "
                    "OR playlist_id NOT IN (SELECT id FROM playlists)").rowcount
                b = conn.execute("DELETE FROM ratings WHERE track_id NOT IN (SELECT id FROM tracks)").rowcount
                results[rid] = a + b
            elif rid == "migrate_stray_dirs":
                conn.commit()
                if migrate_stray:
                    migrate_stray()
                results[rid] = 1
            else:
                results[rid] = 0
        conn.commit()
    finally:
        conn.close()
    return results
